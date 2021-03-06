"""Provides classes for handling API requests."""
# -*- coding: utf-8 -*-
import logging
import os
from collections import OrderedDict

from django.contrib.auth.models import User
from django.db import Error, transaction
from django.http import JsonResponse
from django.utils.translation import ugettext as _

from rest_framework import filters, permissions, status, views, viewsets
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.serializers import ValidationError

from jobs import presets
from jobs.models import (
    ExportConfig, ExportFormat, Job, Region, RegionMask, Tag
)
from jobs.presets import PresetParser, UnfilteredPresetParser
from serializers import (
    ExportConfigSerializer, ExportFormatSerializer, ExportRunSerializer,
    ExportTaskSerializer, JobSerializer, RegionMaskSerializer,
    RegionSerializer, ListJobSerializer
)
from tasks.models import ExportRun, ExportTask
from tasks.task_runners import ExportTaskRunner

from .filters import ExportConfigFilter, ExportRunFilter, JobFilter
from .pagination import LinkHeaderPagination
from .permissions import IsOwnerOrReadOnly
from .renderers import HOTExportApiRenderer
from .validators import validate_bbox_params, validate_search_bbox

# Get an instance of a logger
logger = logging.getLogger(__name__)

# controls how api responses are rendered
renderer_classes = (JSONRenderer, HOTExportApiRenderer)


class JobViewSet(viewsets.ModelViewSet):
    """
    ##Export API Endpoint.

    Main endpoint for export creation and managment. Provides endpoints
    for creating, listing and deleting export jobs.

    Updates to existing jobs are not supported as exports can be cloned.

    Request data can be posted as either `application/x-www-form-urlencoded` or `application/json`.

    **Request parameters**:

    * name (required): The name of the export.
    * description (required): A description of the export.
    * event: The project or event associated with this export, eg Nepal Activation.
    * xmin (required): The minimum longitude coordinate.
    * ymin (required): The minimum latitude coordinate.
    * xmax (required): The maximum longitude coordinate.
    * ymax (required): The maximum latitude coordinate.
    * formats (required): One of the supported export formats ([html](/api/formats) or [json](/api/formats.json)).
        * Use the format `slug` as the value of the formats parameter, eg `formats=thematic&formats=shp`.
    * preset: One of the published preset files ([html](/api/configurations) or [json](/api/configurations.json)).
        * Use the `uid` as the value of the preset parameter, eg `preset=eed84023-6874-4321-9b48-2f7840e76257`.
        * If no preset parameter is provided, then the default [HDM](http://export.hotosm.org/api/hdm-data-model?format=json) tags will be used for the export.
    * published: `true` if this export is to be published globally, `false` otherwise.
        * Unpublished exports will be purged from the system 48 hours after they are created.

    ###Example JSON Request

    This example will create a publicly published export using the default set of HDM tags
    for an area around Dar es Salaam, Tanzania. The export will create thematic shapefile, shapefile and kml files.

    <pre>
        {
            "name": "Dar es Salaam",
            "description": "A description of the test export",
            "event": "A HOT project or activation",
            "xmin": 39.054879,
            "ymin": -7.036697,
            "xmax": 39.484149,
            "ymax": -6.610281,
            "formats": ["thematic", "shp", "kml"],
            "published": "true"
        }
    </pre>

    To create an export with a default set of tags, save the example json request
    to a local file called **request.json** and run the following command from the
    directory where the file is saved. You will need an access token.

    <code>
    curl -v -H "Content-Type: application/json" -H "Authorization: Token [your token]"
    --data @request.json http://export.hotosm.org/api/jobs
    </code>

    To monitor the resulting export run retreive the `uid` value from the returned json
    and call http://export.hotosm.org/api/runs?job_uid=[the returned uid]
    """

    serializer_class = JobSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly)
    parser_classes = (FormParser, MultiPartParser, JSONParser)
    lookup_field = 'uid'
    pagination_class = LinkHeaderPagination
    filter_backends = (filters.DjangoFilterBackend, filters.SearchFilter)
    filter_class = JobFilter
    search_fields = ('name', 'description', 'event', 'user__username', 'region__name')

    def get_queryset(self,):
        """Return all objects by default."""
        return Job.objects.all()

    def list(self, request, *args, **kwargs):
        """
        List export jobs.

        The list of returned exports can be filtered by the **filters.JobFilter**
        and/or by a bounding box extent.

        Args:
            request: the HTTP request.
            *args: Variable length argument list.
            **kwargs: Arbitary keyword arguments.

        Returns:
            A serialized collection of export jobs.
            Uses the **serializers.ListJobSerializer** to
            return a simplified representation of export jobs.

        Raises:
            ValidationError: if the supplied extents are invalid.
        """
        params = self.request.query_params.get('bbox', None)
        if params == None:
            queryset = self.filter_queryset(self.get_queryset())
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = ListJobSerializer(page, many=True, context={'request': request})
                return self.get_paginated_response(serializer.data)
            else:
                serializer = ListJobSerializer(queryset, many=True, context={'request': request})
                return Response(serializer.data)
        if (len(params.split(',')) < 4):
            errors = OrderedDict()
            errors['id'] = _('missing_bbox_parameter')
            errors['message'] = _('Missing bounding box parameter')
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        else:
            extents = params.split(',')
            data = {'xmin': extents[0],
                    'ymin': extents[1],
                    'xmax': extents[2],
                    'ymax': extents[3]
            }
            try:
                bbox_extents = validate_bbox_params(data)
                bbox = validate_search_bbox(bbox_extents)
                queryset = self.filter_queryset(Job.objects.filter(the_geom__within=bbox))
                page = self.paginate_queryset(queryset)
                if page is not None:
                    serializer = ListJobSerializer(page, many=True, context={'request': request})
                    return self.get_paginated_response(serializer.data)
                else:
                    serializer = ListJobSerializer(queryset, many=True, context={'request': request})
                    return Response(serializer.data)
            except ValidationError as e:
                logger.debug(e.detail)
                return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request, *args, **kwargs):
        """
        Create a Job from the supplied request data.

        The request data is validated by *api.serializers.JobSerializer*.
        Associates the *Job* with required *ExportFormats*, *ExportConfig* and *Tags*

        Args:
            request: the HTTP request.
            *args: Variable length argument list.
            **kwargs: Arbitary keyword arguments.

        Returns:
            the newly created Job instance.

        Raises:
            ValidationError: in case of validation errors.
        """
        serializer = self.get_serializer(data=request.data)
        if (serializer.is_valid()):
            """Get the required data from the validated request."""
            formats = request.data.get('formats')
            tags = request.data.get('tags')
            preset = request.data.get('preset')
            translation = request.data.get('translation')
            transform = request.data.get('transform')
            featuresave = request.data.get('featuresave')
            featurepub = request.data.get('featurepub')
            export_formats = []
            job = None
            for slug in formats:
                # would be good to accept either format slug or uuid here..
                try:
                    export_format = ExportFormat.objects.get(slug=slug)
                    export_formats.append(export_format)
                except ExportFormat.DoesNotExist as e:
                    logger.warn('Export format with uid: {0} does not exist'.format(slug))
            if len(export_formats) > 0:
                """Save the job and make sure it's committed before running tasks."""
                try:
                    with transaction.atomic():
                        job = serializer.save()
                        job.formats = export_formats
                        if preset:
                            """Get the tags from the uploaded preset."""
                            logger.debug('Found preset with uid: %s' % preset)
                            config = ExportConfig.objects.get(uid=preset)
                            job.configs.add(config)
                            preset_path = config.upload.path
                            """Use the UnfilteredPresetParser."""
                            parser = presets.UnfilteredPresetParser(preset=preset_path)
                            tags_dict = parser.parse()
                            for entry in tags_dict:
                                tag = Tag.objects.create(
                                    name=entry['name'],
                                    key=entry['key'],
                                    value=entry['value'],
                                    geom_types=entry['geom_types'],
                                    data_model='PRESET',
                                    job=job
                                )
                        elif tags:
                            """Get tags from request."""
                            for entry in tags:
                                tag = Tag.objects.create(
                                    name=entry['name'],
                                    key=entry['key'],
                                    value=entry['value'],
                                    job=job,
                                    data_model=entry['data_model'],
                                    geom_types=entry['geom_types'],
                                    groups=entry['groups']
                                )
                        else:
                            """
                            Use hdm preset as default tags if no preset or tags
                            are provided in the request.
                            """
                            path = os.path.dirname(os.path.realpath(__file__))
                            parser = presets.PresetParser(preset=path + '/presets/hdm_presets.xml')
                            tags_dict = parser.parse()
                            for entry in tags_dict:
                                tag = Tag.objects.create(
                                    name=entry['name'],
                                    key=entry['key'],
                                    value=entry['value'],
                                    geom_types=entry['geom_types'],
                                    data_model='HDM',
                                    job=job
                                )
                        # check for translation file
                        if translation:
                            config = ExportConfig.objects.get(uid=translation)
                            job.configs.add(config)
                        # check for transform file
                        if transform:
                            config = ExportConfig.objects.get(uid=transform)
                            job.configs.add(config)
                except Exception as e:
                    error_data = OrderedDict()
                    error_data['id'] = _('server_error')
                    error_data['message'] = _('Error creating export job: %(error)s') % {'error': e}
                    return Response(error_data, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            else:
                error_data = OrderedDict()
                error_data['formats'] = [_('Invalid format provided.')]
                return Response(error_data, status=status.HTTP_400_BAD_REQUEST)

            # run the tasks
            task_runner = ExportTaskRunner()
            job_uid = str(job.uid)
            task_runner.run_task(job_uid=job_uid)
            running = JobSerializer(job, context={'request': request})
            return Response(running.data, status=status.HTTP_202_ACCEPTED)
        else:
            return Response(serializer.errors,
                            status=status.HTTP_400_BAD_REQUEST)


class RunJob(views.APIView):
    """
    ##Re-run Export

    Re-runs an export job for the given `job_uid`: `/api/rerun?job_uid=<job_uid>`
    """

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, uid=None, format=None):
        """
        Re-runs the job.

        Gets the job_uid and current user from the request.
        Creates an instance of the ExportTaskRunner and
        calls run_task on it, passing the job_uid and user.

        Args:
            the http request

        Returns:
            the serialized run data.
        """
        job_uid = request.query_params.get('job_uid', None)
        user = request.user
        if (job_uid):
            # run the tasks
            job = Job.objects.get(uid=job_uid)
            task_runner = ExportTaskRunner()
            run = task_runner.run_task(job_uid=job_uid, user=user)
            if run:
                running = ExportRunSerializer(run, context={'request': request})
                return Response(running.data, status=status.HTTP_202_ACCEPTED)
            else:
                return Response([{'detail': _('Failed to run Export')}], status.HTTP_400_BAD_REQUEST)
        else:
            return Response([{'detail': _('Export not found')}], status.HTTP_404_NOT_FOUND)


class ExportFormatViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ###ExportFormat API endpoint.

    Endpoint exposing the supported export formats.
    """
    serializer_class = ExportFormatSerializer
    permission_classes = (permissions.AllowAny,)
    queryset = ExportFormat.objects.all()
    lookup_field = 'slug'
    ordering = ['description']


class RegionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ###Region API endpoint.

    Endpoint exposing the supported regions.
    """
    serializer_class = RegionSerializer
    permission_classes = (permissions.AllowAny,)
    queryset = Region.objects.all()
    lookup_field = 'uid'


class RegionMaskViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ###Region Mask API Endpoint.

    Return a MULTIPOLYGON representing the mask of the
    HOT Regions as a GeoJSON Feature Collection.
    """
    serializer_class = RegionMaskSerializer
    permission_classes = (permissions.AllowAny,)
    queryset = RegionMask.objects.all()


class ExportRunViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ###Export Run API Endpoint.

    Provides an endpoint for querying export runs.
    Export runs for a particular job can be filtered by status by appending one of
    `COMPLETED`, `SUBMITTED`, `INCOMPLETE` or `FAILED` as the value of the `STATUS` parameter:
    `/api/runs?job_uid=a_job_uid&status=STATUS`
    """
    serializer_class = ExportRunSerializer
    permission_classes = (permissions.AllowAny,)
    filter_backends = (filters.DjangoFilterBackend,)
    filter_class = ExportRunFilter
    lookup_field = 'uid'

    def get_queryset(self):
        return ExportRun.objects.all().order_by('-started_at')

    def retrieve(self, request, uid=None, *args, **kwargs):
        """
        Get an ExportRun.

        Gets the run_uid from the request and returns run data for the
        associated ExportRun.

        Args:
            request: the http request.
            uid: the run uid.

        Returns:
            the serialized run data.
        """
        queryset = ExportRun.objects.filter(uid=uid)
        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def list(self, request, *args, **kwargs):
        """
        List the ExportRuns for a single Job.

        Gets the job_uid from the request and returns run data for the
        associated Job.

        Args:
            the http request.

        Returns:
            the serialized run data.
        """
        job_uid = self.request.query_params.get('job_uid', None)
        queryset = self.filter_queryset(ExportRun.objects.filter(job__uid=job_uid).order_by('-started_at'))
        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class ExportConfigViewSet(viewsets.ModelViewSet):
    """
    Endpoint for operations on export configurations.

    Lists all available configuration files.
    """
    serializer_class = ExportConfigSerializer
    pagination_class = LinkHeaderPagination
    filter_backends = (filters.DjangoFilterBackend, filters.SearchFilter)
    filter_class = ExportConfigFilter
    search_fields = ('name', 'config_type', 'user__username')
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,
                          IsOwnerOrReadOnly)
    parser_classes = (FormParser, MultiPartParser, JSONParser)
    queryset = ExportConfig.objects.filter(config_type='PRESET')
    lookup_field = 'uid'


class ExportTaskViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ###ExportTask API endpoint.

    Provides List and Retrieve endpoints for ExportTasks.
    """
    serializer_class = ExportTaskSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)
    queryset = ExportTask.objects.all()
    lookup_field = 'uid'

    def retrieve(self, request, uid=None, *args, **kwargs):
        """
        GET a single export task.

        Args:
            request: the http request.
            uid: the uid of the export task to GET.
        Returns:
            the serialized ExportTask data.
        """
        queryset = ExportTask.objects.filter(uid=uid)
        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class PresetViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Returns the list of PRESET configuration files.
    """
    CONFIG_TYPE = 'PRESET'
    serializer_class = ExportConfigSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)
    queryset = ExportConfig.objects.filter(config_type=CONFIG_TYPE)
    lookup_field = 'uid'


class TranslationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Return the list of TRANSLATION configuration files.
    """
    CONFIG_TYPE = 'TRANSLATION'
    serializer_class = ExportConfigSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)
    queryset = ExportConfig.objects.filter(config_type=CONFIG_TYPE)
    lookup_field = 'uid'


class TransformViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Return the list of TRANSFORM configuration files.
    """
    CONFIG_TYPE = 'TRANSFORM'
    serializer_class = ExportConfigSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)
    queryset = ExportConfig.objects.filter(config_type=CONFIG_TYPE)
    lookup_field = 'uid'


class HDMDataModelView(views.APIView):
    """Endpoint exposing the HDM Data Model."""
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    def get(self, request, format='json'):
        path = os.path.dirname(os.path.realpath(__file__))
        parser = PresetParser(path + '/presets/hdm_presets.xml')
        data = parser.build_hdm_preset_dict()
        return JsonResponse(data, status=status.HTTP_200_OK)


class OSMDataModelView(views.APIView):
    """Endpoint exposing the OSM Data Model."""
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    def get(self, request, format='json'):
        path = os.path.dirname(os.path.realpath(__file__))
        parser = PresetParser(path + '/presets/osm_presets.xml')
        data = parser.build_hdm_preset_dict()
        return JsonResponse(data, status=status.HTTP_200_OK)
