# Copyright 2017 IBM Corp
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

from collections import defaultdict
import re

from oslo_log import log as logging
from zun.common import exception
from zun.common import utils
from zun.container.os_capability import host_capability


LOG = logging.getLogger(__name__)


class LinuxHost(host_capability.Host):

    def get_cpu_numa_info(self):
        # TODO(sbiswas7): rootwrap changes for zun required.
        try:
            output = utils.execute('numactl', '-H')
        except exception.CommandError:
            LOG.info("There was a problem while executing lscpu -p=socket"
                     ",cpu,online. Try again without the online column.")
        nodes = []
        for line in re.split('\n', output[0]):
            if re.match('node.*cpus.*', line):
                nodes.append(line)
        node_map = defaultdict(list)
        for node in nodes:
            node_id = re.findall("node (\d+) cpus", node)[0]
            node_cpus = re.findall("cpus\: (.*)", node)
            node_cpus_list = node_cpus[0].split(" ")
            for num in range(len(node_cpus_list)):
                node_map[node_id].append(int(node_cpus_list[num]))
        return node_map

    def get_mem_numa_info(self):
        try:
            output = utils.execute('numactl', '-H')
        except exception.CommandError:
            LOG.info(
                "There was a problem while executing numactl -H,"
                "Try again without the online column.")

        sizes = re.findall("size\: \d*", str(output))
        mem_numa = []
        for size in sizes:
            mem_numa.append(int(size.split(' ')[1]))
        return mem_numa
