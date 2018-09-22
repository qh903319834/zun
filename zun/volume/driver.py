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

import abc
import functools
import shutil
import six

from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import fileutils
from stevedore import driver as stevedore_driver

from zun.common import exception
from zun.common.i18n import _
from zun.common import mount
from zun.common import utils
import zun.conf
from zun.volume import cinder_api
from zun.volume import cinder_workflow

LOG = logging.getLogger(__name__)

CONF = zun.conf.CONF


def driver(driver_name, *args, **kwargs):
    LOG.info("Loading volume driver '%s'", driver_name)
    volume_driver = stevedore_driver.DriverManager(
        "zun.volume.driver",
        driver_name,
        invoke_on_load=True,
        invoke_args=args,
        invoke_kwds=kwargs).driver
    if not isinstance(volume_driver, VolumeDriver):
        raise exception.ZunException(_("Invalid volume driver type"))
    return volume_driver


def validate_volume_provider(supported_providers):
    """Wraps a method to validate volume provider."""

    def decorator(function):
        @functools.wraps(function)
        def decorated_function(self, context, volume, **kwargs):
            provider = volume.volume_provider
            if provider not in supported_providers:
                msg = _("The volume provider '%s' is not supported") % provider
                raise exception.ZunException(msg)

            return function(self, context, volume, **kwargs)

        return decorated_function
    return decorator


@six.add_metaclass(abc.ABCMeta)
class VolumeDriver(object):
    """The base class that all Volume classes should inherit from."""

    def attach(self, *args, **kwargs):
        raise NotImplementedError()

    def detach(self, *args, **kwargs):
        raise NotImplementedError()

    def delete(self, *args, **kwargs):
        raise NotImplementedError()

    def bind_mount(self, *args, **kwargs):
        raise NotImplementedError()

    def is_volume_available(self, context, volume):
        raise NotImplementedError()

    def is_volume_deleted(self, context, volume):
        raise NotImplementedError()


class Local(VolumeDriver):

    supported_providers = ['local']

    @validate_volume_provider(supported_providers)
    def attach(self, context, volume):
        mountpoint = mount.get_mountpoint(volume.uuid)
        fileutils.ensure_tree(mountpoint)
        filename = '/'.join([mountpoint, volume.uuid])
        with open(filename, 'wb') as fd:
            content = utils.decode_file_data(volume.contents)
            fd.write(content)

    def _remove_local_file(self, volume):
        mountpoint = mount.get_mountpoint(volume.uuid)
        shutil.rmtree(mountpoint)

    @validate_volume_provider(supported_providers)
    def detach(self, context, volume):
        self._remove_local_file(volume)

    @validate_volume_provider(supported_providers)
    def delete(self, context, volume):
        self._remove_local_file(volume)

    @validate_volume_provider(supported_providers)
    def bind_mount(self, context, volume):
        mountpoint = mount.get_mountpoint(volume.uuid)
        filename = '/'.join([mountpoint, volume.uuid])
        return filename, volume.container_path

    def is_volume_available(self, context, volume):
        return True, False


class Cinder(VolumeDriver):

    supported_providers = [
        'cinder'
    ]

    @validate_volume_provider(supported_providers)
    def attach(self, context, volume):
        cinder = cinder_workflow.CinderWorkflow(context)
        devpath = cinder.attach_volume(volume)
        try:
            self._mount_device(volume, devpath)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("Failed to mount device")
                try:
                    cinder.detach_volume(volume)
                except Exception:
                    LOG.exception("Failed to detach volume")

    def _mount_device(self, volume, devpath):
        mountpoint = mount.get_mountpoint(volume.uuid)
        fileutils.ensure_tree(mountpoint)
        mount.do_mount(devpath, mountpoint, CONF.volume.fstype)

    @validate_volume_provider(supported_providers)
    def detach(self, context, volume):
        self._unmount_device(volume)
        cinder = cinder_workflow.CinderWorkflow(context)
        cinder.detach_volume(context, volume)

    @validate_volume_provider(supported_providers)
    def delete(self, context, volume):
        cinder = cinder_workflow.CinderWorkflow(context)
        cinder.delete_volume(volume)

    def _unmount_device(self, volume):
        if hasattr(volume, 'connection_info'):
            mountpoint = mount.get_mountpoint(volume.uuid)
            mount.do_unmount(mountpoint)
            shutil.rmtree(mountpoint)

    @validate_volume_provider(supported_providers)
    def bind_mount(self, context, volume):
        mountpoint = mount.get_mountpoint(volume.uuid)
        return mountpoint, volume.container_path

    @validate_volume_provider(supported_providers)
    def get_volume_status(self, context, volume):
        ca = cinder_api.CinderAPI(context)
        return ca.get(volume.cinder_volume_id).status

    @validate_volume_provider(supported_providers)
    def check_multiattach(self, context, volume):
        ca = cinder_api.CinderAPI(context)
        return ca.get(volume.cinder_volume_id).multiattach

    @validate_volume_provider(supported_providers)
    def is_volume_available(self, context, volume):
        status = self.get_volume_status(context, volume)
        if status == 'available':
            is_available = True
            is_error = False
        elif status == 'in-use':
            multiattach = self.check_multiattach(context, volume)
            is_available = multiattach
            is_error = False
        elif status == 'error':
            is_available = False
            is_error = True
        else:
            is_available = False
            is_error = False

        return is_available, is_error

    @validate_volume_provider(supported_providers)
    def is_volume_deleted(self, context, volume):
        try:
            volume = cinder_api.CinderAPI(context).search_volume(
                volume.cinder_volume_id)
            is_deleted = False
            # Cinder volume error states: 'error', 'error_deleting',
            # 'error_backing-up', 'error_restoring', 'error_extending',
            # all of which start with 'error'
            is_error = True if 'error' in volume.status else False
        except exception.VolumeNotFound:
            is_deleted = True
            is_error = False

        return is_deleted, is_error
