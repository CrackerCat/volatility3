# This file is Copyright 2021 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#
# Author: Gustavo Moreira

import logging
from collections import namedtuple
from typing import Tuple, List, Iterable, Union

from volatility3.framework import renderers, interfaces
from volatility3.framework.configuration import requirements
from volatility3.framework.interfaces import plugins
from volatility3.plugins.linux import pslist

vollog = logging.getLogger(__name__)

MountInfoData = namedtuple("MountInfoData", ("mnt_id", "parent_id", "st_dev", "mnt_root_path", "path_root",
                                             "mnt_opts", "fields", "mnt_type", "devname", "sb_opts"))

class MountInfo(plugins.PluginInterface):
    """Lists mount points on processes mount namespaces"""

    _required_framework_version = (2, 2, 0)

    _version = (1, 0, 0)

    @classmethod
    def get_requirements(cls) -> List[interfaces.configuration.RequirementInterface]:
        return [
            requirements.ModuleRequirement(name="kernel", description="Linux kernel",
                                           architectures=["Intel32", "Intel64"]),
            requirements.PluginRequirement(name="pslist",
                                           plugin=pslist.PsList, version=(2, 0, 0)),
            requirements.ListRequirement(name="pids",
                                         description="Filter on specific process IDs.",
                                         element_type=int,
                                         optional=True),
            requirements.ListRequirement(name="mntns",
                                         description="Filter results by mount namespace. "
                                                     "Otherwise, all of them are shown.",
                                         element_type=int,
                                         optional=True),
            requirements.BooleanRequirement(name="mount-format",
                                            description="Shows a brief summary of the mount points information "
                                            "with similar output format to the older /proc/[pid]/mounts or the "
                                            "user-land command 'mount -l'.",
                                            optional=True,
                                            default=False),
        ]

    @classmethod
    def _do_get_path(cls, mnt, fs_root) -> Union[None, str]:
        """It mimics the Linux kernel prepend_path function."""
        vfsmnt = mnt.mnt
        dentry = vfsmnt.get_mnt_root()

        path_reversed = []
        while dentry != fs_root.dentry or vfsmnt.vol.offset != fs_root.mnt:
            if dentry == vfsmnt.get_mnt_root() or dentry.is_root():
                parent = mnt.get_mnt_parent().dereference()
                # Escaped?
                if dentry != vfsmnt.get_mnt_root():
                    return None

                # Global root?
                if mnt.vol.offset != parent.vol.offset:
                    dentry = mnt.get_mnt_mountpoint()
                    mnt = parent
                    vfsmnt = mnt.mnt
                    continue

                return None

            parent = dentry.d_parent
            dname = dentry.d_name.name_as_str()
            path_reversed.append(dname.strip("/"))
            dentry = parent

        path = "/" + "/".join(reversed(path_reversed))
        return path

    @classmethod
    def get_mountinfo(cls, mnt, task) -> Union[None, Tuple[int, int, str, str, str, List[str],
                                                           List[str], str, str, List[str]]]:
        """Extract various information about a mount point.
        It mimics the Linux kernel show_mountinfo function.
        """
        mnt_root = mnt.get_mnt_root()
        if not mnt_root:
            return None

        path_root = cls._do_get_path(mnt, task.fs.root)
        if path_root is None:
            return None

        mnt_root_path = mnt_root.path()
        superblock = mnt.get_mnt_sb()

        mnt_id: int = mnt.mnt_id
        parent_id: int = mnt.mnt_parent.mnt_id

        st_dev = f"{superblock.major}:{superblock.minor}"

        mnt_opts: List[str] = []
        mnt_opts.append(mnt.get_flags_access())
        mnt_opts.extend(mnt.get_flags_opts())

        # Tagged fields
        fields: List[str] = []
        if mnt.is_shared():
            fields.append(f"shared:{mnt.mnt_group_id}")

        if mnt.is_slave():
            master = mnt.mnt_master.mnt_group_id
            fields.append(f"master:{master}")
            dominating_id = mnt.get_dominating_id(task.fs.root)
            if dominating_id and dominating_id != master:
                fields.append(f"propagate_from:{dominating_id}")

        if mnt.is_unbindable():
            fields.append("unbindable")

        mnt_type = superblock.get_type()

        devname = mnt.get_devname()
        if not devname:
            devname = "none"

        sb_opts: List[str] = []
        sb_opts.append(superblock.get_flags_access())
        sb_opts.extend(superblock.get_flags_opts())

        return MountInfoData(mnt_id, parent_id, st_dev, mnt_root_path, path_root, mnt_opts, fields,
                             mnt_type, devname, sb_opts)

    def _get_tasks_mountpoints(self, tasks: Iterable[interfaces.objects.ObjectInterface], per_namespace: bool):
        seen_namespaces = set()
        for task in tasks:
            if not (task and task.fs and task.fs.root and task.nsproxy and task.nsproxy.mnt_ns):
                # This task doesn't have all the information required
                continue

            mnt_namespace = task.nsproxy.mnt_ns
            mnt_ns_id = mnt_namespace.get_inode()

            if per_namespace:
                if mnt_ns_id in seen_namespaces:
                    continue
                else:
                    seen_namespaces.add(mnt_ns_id)

            for mount in mnt_namespace.get_mount_points():
                yield task, mount, mnt_ns_id

    def _generator(
            self,
            tasks: Iterable[interfaces.objects.ObjectInterface],
            mnt_ns_ids: List[int],
            mount_format: bool,
            per_namespace: bool) -> Iterable[Tuple[int, Tuple]]:

        for task, mnt, mnt_ns_id in self._get_tasks_mountpoints(tasks, per_namespace):
            if mnt_ns_ids and mnt_ns_id not in mnt_ns_ids:
                continue

            mnt_info = self.get_mountinfo(mnt, task)
            if mnt_info is None:
                continue

            if mount_format:
                all_opts = set()
                all_opts.update(mnt_info.mnt_opts)
                all_opts.update(mnt_info.sb_opts)
                all_opts_str = ",".join(all_opts)

                extra_fields_values = [mnt_info.devname, mnt_info.path_root, mnt_info.mnt_type, all_opts_str]
            else:
                mnt_opts_str = ",".join(mnt_info.mnt_opts)
                fields_str = " ".join(mnt_info.fields)
                sb_opts_str = ",".join(mnt_info.sb_opts)

                extra_fields_values = [mnt_info.mnt_id, mnt_info.parent_id, mnt_info.st_dev, mnt_info.mnt_root_path,
                                       mnt_info.path_root, mnt_opts_str, fields_str, mnt_info.mnt_type,
                                       mnt_info.devname, sb_opts_str]

            fields_values = [mnt_ns_id]
            if not per_namespace:
                fields_values.append(task.pid)
            fields_values.extend(extra_fields_values)

            yield (0, fields_values)

    def run(self):
        pids = self.config.get('pids')
        mount_ns_ids = self.config.get('mntns')
        mount_format = self.config.get('mount-format')

        pid_filter = pslist.PsList.create_pid_filter(pids)
        tasks = pslist.PsList.list_tasks(self.context, self.config['kernel'], filter_func=pid_filter)

        columns = [("MNT_NS_ID", int)]
        # The PID column does not make sense when a PID filter is not specified. In that case, the default behavior is
        # to displays the mountpoints per namespace.
        if pids:
            columns.append(("PID", int))
            per_namespace = False
        else:
            per_namespace = True

        if self.config.get('mount-format'):
            extra_columns = [("DEVNAME", str), ("PATH", str), ("FSTYPE", str), ("MNT_OPTS", str)]
        else:
            # /proc/[pid]/mountinfo output format
            extra_columns = [("MOUNT ID", int), ("PARENT_ID", int), ("MAJOR:MINOR", str), ("ROOT", str),
                             ("MOUNT_POINT", str), ("MOUNT_OPTIONS", str), ("FIELDS", str), ("FSTYPE", str),
                             ("MOUNT_SRC", str), ("SB_OPTIONS", str)]

        columns.extend(extra_columns)

        return renderers.TreeGrid(columns, self._generator(tasks, mount_ns_ids, mount_format, per_namespace))
