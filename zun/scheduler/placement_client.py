#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import functools
import zun.conf

from keystoneauth1 import exceptions as ks_exc
from keystoneauth1 import loading as keystone
from oslo_log import log as logging
from zun.common.i18n import _

CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)
WARN_EVERY = 10


def warn_limit(self, msg):
    if self._warn_count:
        self._warn_count -= 1
    else:
        self._warn_count = WARN_EVERY
        LOG.warning(msg)


def safe_connect(f):
    @functools.wraps(f)
    def wrapper(self, *a, **k):
        try:
            return f(self, *a, **k)
        except ks_exc.EndpointNotFound:
            warn_limit(
                self,
                _('The placement API endpoint not found. Placement is '
                  'optional in Newton, but required in Ocata. Please '
                  'enable the placement service before upgrading.'))
        except ks_exc.MissingAuthPlugin:
            warn_limit(
                self,
                _('No authentication information found for placement '
                  'API. Placement is optional in Newton, but required '
                  'in Ocata. Please enable the placement service '
                  'before upgrading.'))
        except ks_exc.Unauthorized:
            warn_limit(
                self,
                _('Placement service credentials do not work. '
                  'Placement is optional in Newton, but required '
                  'in Ocata. Please enable the placement service '
                  'before upgrading.'))
        except ks_exc.DiscoveryFailure:
            # TODO(_gryf): Looks like DiscoveryFailure is not the only missing
            # exception here. In Pike we should take care about keystoneauth1
            # failures handling globally.
            warn_limit(self,
                       _('Discovering suitable URL for placement API '
                         'failed.'))
        except ks_exc.ConnectFailure:
            msg = _('Placement API service is not responding.')
            LOG.warning(msg)
    return wrapper


def _merge_resources(original_resources, new_resources, sign=1):
    """Merge a list of new resources with existing resources.

    Either add the resources (if sign is 1) or subtract (if sign is -1).
    If the resulting value is 0 do not include the resource in the results.
    """

    all_keys = set(original_resources.keys()) | set(new_resources.keys())
    for key in all_keys:
        value = (original_resources.get(key, 0) +
                 (sign * new_resources.get(key, 0)))
        if value:
            original_resources[key] = value
        else:
            original_resources.pop(key, None)


def _move_operation_alloc_request(source_allocs, dest_alloc_req):
    """_move_operation_alloc_request

    Given existing allocations for a source host and a new allocation
    request for a destination host, return a new allocation request that
    contains resources claimed against both source and destination, accounting
    for shared providers.

    Also accounts for a resize to the same host where the source and dest
    compute node resource providers are going to be the same. In that case
    we sum the resource allocations for the single provider.
    :param source_allocs: Dict, keyed by resource provider UUID, of resources
                          allocated on the source host
    :param dest_alloc_request: The allocation request for resources against the
                               destination host
    """
    LOG.debug("Doubling-up allocation request for move operation.")
    # Remove any allocations against resource providers that are
    # already allocated against on the source host (like shared storage
    # providers)
    cur_rp_uuids = set(source_allocs.keys())
    new_rp_uuids = set(a['resource_provider']['uuid']
                       for a in dest_alloc_req['allocations']) - cur_rp_uuids

    current_allocs = [
        {
            'resource_provider': {
                'uuid': cur_rp_uuid,
            },
            'resources': alloc['resources'],
        } for cur_rp_uuid, alloc in source_allocs.items()
    ]
    new_alloc_req = {'allocations': current_allocs}
    for alloc in dest_alloc_req['allocations']:
        if alloc['resource_provider']['uuid'] in new_rp_uuids:
            new_alloc_req['allocations'].append(alloc)
        elif not new_rp_uuids:
            # If there are no new_rp_uuids that means we're resizing to
            # the same host so we need to sum the allocations for
            # the compute node (and possibly shared providers) using both
            # the current and new allocations.
            # Note that we sum the allocations rather than take the max per
            # resource class between the current and new allocations because
            # the compute node/resource tracker is going to adjust for
            # decrementing any old allocations as necessary, the scheduler
            # shouldn't make assumptions about that.
            for current_alloc in current_allocs:
                # Find the matching resource provider allocations by UUID.
                if (current_alloc['resource_provider']['uuid'] ==
                        alloc['resource_provider']['uuid']):
                    # Now sum the current allocation resource amounts with
                    # the new allocation resource amounts.
                    _merge_resources(current_alloc['resources'],
                                     alloc['resources'])

    LOG.debug("New allocation request containing both source and "
              "destination hosts in move operation: %s", new_alloc_req)
    return new_alloc_req


def get_placement_request_id(response):
    if response is not None:
        return response.headers.get(
            'openstack-request-id',
            response.headers.get('x-openstack-request-id'))


class SchedulerReportClient(object):
    """Client class for updating the scheduler."""

    def __init__(self):
        # A dict, keyed by the resource provider UUID, of ResourceProvider
        # objects that will have their inventories and allocations tracked by
        # the placement API for the compute host
        self._resource_providers = {}
        # A dict, keyed by resource provider UUID, of sets of aggregate UUIDs
        # the provider is associated with
        self._provider_aggregate_map = {}
        # A dict, keyed by resource provider UUID, of sets of traits UUIDs
        # the provider is associated with
        self._provider_traits_map = {}
        auth_plugin = keystone.load_auth_from_conf_options(
            CONF, 'placement')
        self._client = keystone.load_session_from_conf_options(
            CONF, 'placement', auth=auth_plugin)
        self._warn_count = 0
        self.ks_filter = {'service_type': 'placement',
                          'region_name': CONF.placement.os_region_name,
                          'interface': CONF.placement.os_interface}

    def get(self, url, version=None):
        kwargs = {}
        if version is not None:
            # TODO(mriedem): Perform some version discovery at some point.
            kwargs = {
                'headers': {
                    'OpenStack-API-Version': 'placement %s' % version
                },
            }
        return self._client.get(
            url,
            endpoint_filter=self.ks_filter, raise_exc=False, **kwargs)

    def post(self, url, data, version=None):
        # NOTE(sdague): using json= instead of data= sets the
        # media type to application/json for us. Placement API is
        # more sensitive to this than other APIs in the OpenStack
        # ecosystem.
        kwargs = {}
        if version is not None:
            # TODO(mriedem): Perform some version discovery at some point.
            kwargs = {
                'headers': {
                    'OpenStack-API-Version': 'placement %s' % version
                },
            }
        return self._client.post(
            url, json=data,
            endpoint_filter=self.ks_filter, raise_exc=False, **kwargs)

    def put(self, url, data, version=None):
        # NOTE(sdague): using json= instead of data= sets the
        # media type to application/json for us. Placement API is
        # more sensitive to this than other APIs in the OpenStack
        # ecosystem.
        kwargs = {}
        if version is not None:
            # TODO(mriedem): Perform some version discovery at some point.
            kwargs = {
                'headers': {
                    'OpenStack-API-Version': 'placement %s' % version
                },
            }
        if data:
            kwargs['json'] = data
        return self._client.put(
            url, endpoint_filter=self.ks_filter, raise_exc=False,
            **kwargs)

    def delete(self, url, version=None):
        kwargs = {}
        if version is not None:
            # TODO(mriedem): Perform some version discovery at some point.
            kwargs = {
                'headers': {
                    'OpenStack-API-Version': 'placement %s' % version
                },
            }
        return self._client.delete(
            url,
            endpoint_filter=self.ks_filter, raise_exc=False, **kwargs)

    @safe_connect
    def update_numa_topology(self, compute_node):
        numa_topology = []
        for node in reversed(compute_node.numa_topology.nodes):
            numa_topology.append(
                dict(memory_usage=node.mem_total - node.mem_available,
                     memory=node.mem_total,
                     cpuset=list(node.cpuset),
                     pinned_cpus=list(node.pinned_cpus),
                     cpu_usage=len(node.pinned_cpus),
                     id=node.id))
        rp_uuid = compute_node.uuid
        payload = {
            'zun_numa_topology': numa_topology,
            'uuid': rp_uuid,
        }
        print('placement_client.py.update_numa_topo_payload', payload)
        url = '/resource_providers/%s/numa_topologies' % rp_uuid
        r = self.put(url, payload, version='1.15', )
        if r.status_code != 200:
            LOG.warning(
                'Unable to submit zun numa topology for instance '
                '%(uuid)s (%(code)i %(text)s)',
                {'uuid': rp_uuid,
                 'code': r.status_code,
                 'text': r.text})
        return r.status_code == 200
