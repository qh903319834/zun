# Copyright (c) 2018 ECAEPNEL
# All Rights Reserved.
#
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

from oslo_log import log as logging

from zun.scheduler import filters

LOG = logging.getLogger(__name__)


class CPUSETFilter(filters.BaseHostFilter):
    """Filter the host by cpu and memory request of cpuset"""

    run_filter_once_per_request = True

    def host_passes(self, host_state, container, extra_spec):
        if container.cpu_policy == 'dedicated':
            for node in reversed(host_state.numa_topology.nodes):
                len_cpu = len(node.cpuset - node.pinned_cpus) - container.cpu
                len_mem = node.mem_available - int(container.memory[:-1])
                if len_cpu >= 0 and len_mem >= 0:
                    host_state.limits['cpuset'] = {
                        'node': node.id,
                        'cpuset_cpu': node.cpuset,
                        'cpuset_cpu_pinned': node.pinned_cpus,
                        'cpuset_mem': node.mem_available}
                    host_state.limits['cpu'] = host_state.cpus
                    host_state.limits['memory'] = host_state.mem_total
                    return True
            return False
        else:
            return True
