#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2007, 2012 Red Hat, Inc
# Michael DeHaan <michael.dehaan@gmail.com>
# Seth Vidal <skvidal@fedoraproject.org>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type


DOCUMENTATION = '''
---
module: virt
short_description: Manages virtual machines supported by libvirt
description:
     - Manages virtual machines supported by I(libvirt).
options:
    flags:
        choices: [ 'managed_save', 'snapshots_metadata', 'nvram', 'keep_nvram', 'checkpoints_metadata']
        description:
            - Pass additional parameters.
            - Currently only implemented with command C(undefine).
              Specify which metadata should be removed with C(undefine).
              Useful option to be able to C(undefine) guests with UEFI nvram.
              C(nvram) and C(keep_nvram) are conflicting and mutually exclusive.
              Consider option C(force) if all related metadata should be removed.
        type: list
        elements: str
    force:
        description:
            - Enforce an action.
            - Currently only implemented with command C(undefine).
              This option can be used instead of providing all C(flags).
              If C(yes), C(undefine) removes also any related nvram or other metadata, if existing.
              If C(no) or not set, C(undefine) executes only if there is no nvram or other metadata existing.
              Otherwise the task fails and the guest is kept defined without change.
              C(yes) and option C(flags) should not be provided together. In this case
              C(undefine) ignores C(yes), considers only C(flags) and issues a warning.
        type: bool
extends_documentation_fragment:
    - community.libvirt.virt.options_uri
    - community.libvirt.virt.options_xml
    - community.libvirt.virt.options_guest
    - community.libvirt.virt.options_autostart
    - community.libvirt.virt.options_state
    - community.libvirt.virt.options_command
    - community.libvirt.requirements
author:
    - Ansible Core Team
    - Michael DeHaan
    - Seth Vidal (@skvidal)
'''

EXAMPLES = '''
# a playbook task line:
- name: Start a VM
  community.libvirt.virt:
    name: alpha
    state: running

# /usr/bin/ansible invocations
# ansible host -m virt -a "name=alpha command=status"
# ansible host -m virt -a "name=alpha command=get_xml"
# ansible host -m virt -a "name=alpha command=create uri=lxc:///"

# defining and launching an LXC guest
- name: Define a VM
  community.libvirt.virt:
    command: define
    xml: "{{ lookup('template', 'container-template.xml.j2') }}"
    uri: 'lxc:///'
- name: start vm
  community.libvirt.virt:
    name: foo
    state: running
    uri: 'lxc:///'

# setting autostart on a qemu VM (default uri)
- name: Set autostart for a VM
  community.libvirt.virt:
    name: foo
    autostart: yes

# Defining a VM and making is autostart with host. VM will be off after this task
- name: Define vm from xml and set autostart
  community.libvirt.virt:
    command: define
    xml: "{{ lookup('template', 'vm_template.xml.j2') }}"
    autostart: yes

# Undefine VM only, if it has no existing nvram or other metadata
- name: Undefine qemu VM
  community.libvirt.virt:
    name: foo

# Undefine VM and force remove all of its related metadata (nvram, snapshots, etc.)
- name: "Undefine qemu VM with force"
  community.libvirt.virt:
    name: foo
    force: yes

# Undefine VM and remove all of its specified metadata specified
# Result would the same as with force=true
- name: Undefine qemu VM with list of flags
  community.libvirt.virt:
    name: foo
    flags: managed_save, snapshots_metadata, nvram, checkpoints_metadata

# Undefine VM, but keep its nvram
- name: Undefine qemu VM and keep its nvram
  community.libvirt.virt:
    name: foo
    flags: keep_nvram

# Listing VMs
- name: List all VMs
  community.libvirt.virt:
    command: list_vms
  register: all_vms

- name: List only running VMs
  community.libvirt.virt:
    command: list_vms
    state: running
  register: running_vms
'''

RETURN = '''
# for list_vms command
list_vms:
    description: The list of vms defined on the remote system.
    type: list
    returned: success
    sample: [
        "build.example.org",
        "dev.example.org"
    ]
# for status command
status:
    description: The status of the VM, among running, crashed, paused and shutdown.
    type: str
    sample: "success"
    returned: success
'''

import traceback

try:
    import libvirt
    from libvirt import libvirtError
except ImportError:
    HAS_VIRT = False
else:
    HAS_VIRT = True

import re

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils._text import to_native


VIRT_FAILED = 1
VIRT_SUCCESS = 0
VIRT_UNAVAILABLE = 2

ALL_COMMANDS = []
VM_COMMANDS = ['create', 'define', 'destroy', 'get_xml', 'pause', 'shutdown', 'status', 'start', 'stop', 'undefine', 'unpause']
HOST_COMMANDS = ['freemem', 'info', 'list_vms', 'nodeinfo', 'virttype']
ALL_COMMANDS.extend(VM_COMMANDS)
ALL_COMMANDS.extend(HOST_COMMANDS)

VIRT_STATE_NAME_MAP = {
    0: 'running',
    1: 'running',
    2: 'running',
    3: 'paused',
    4: 'shutdown',
    5: 'shutdown',
    6: 'crashed',
}

ENTRY_UNDEFINE_FLAGS_MAP = {
    'managed_save': 1,
    'snapshots_metadata': 2,
    'nvram': 4,
    'keep_nvram': 8,
    'checkpoints_metadata': 16,
}

ALL_FLAGS = []
ALL_FLAGS.extend(ENTRY_UNDEFINE_FLAGS_MAP.keys())


class VMNotFound(Exception):
    pass


class LibvirtConnection(object):

    def __init__(self, uri, module):

        self.module = module

        cmd = "uname -r"
        rc, stdout, stderr = self.module.run_command(cmd)

        if "xen" in stdout:
            conn = libvirt.open(None)
        elif "esx" in uri:
            auth = [[libvirt.VIR_CRED_AUTHNAME, libvirt.VIR_CRED_NOECHOPROMPT], [], None]
            conn = libvirt.openAuth(uri, auth)
        else:
            conn = libvirt.open(uri)

        if not conn:
            raise Exception("hypervisor connection failure")

        self.conn = conn

    def find_vm(self, vmid):
        """
        Extra bonus feature: vmid = -1 returns a list of everything
        """

        vms = self.conn.listAllDomains()

        if vmid == -1:
            return vms

        for vm in vms:
            if vm.name() == vmid:
                return vm

        raise VMNotFound("virtual machine %s not found" % vmid)

    def shutdown(self, vmid):
        return self.find_vm(vmid).shutdown()

    def pause(self, vmid):
        return self.suspend(vmid)

    def unpause(self, vmid):
        return self.resume(vmid)

    def suspend(self, vmid):
        return self.find_vm(vmid).suspend()

    def resume(self, vmid):
        return self.find_vm(vmid).resume()

    def create(self, vmid):
        return self.find_vm(vmid).create()

    def destroy(self, vmid):
        return self.find_vm(vmid).destroy()

    def undefine(self, vmid, flag):
        return self.find_vm(vmid).undefineFlags(flag)

    def get_status2(self, vm):
        state = vm.info()[0]
        return VIRT_STATE_NAME_MAP.get(state, "unknown")

    def get_status(self, vmid):
        state = self.find_vm(vmid).info()[0]
        return VIRT_STATE_NAME_MAP.get(state, "unknown")

    def nodeinfo(self):
        return self.conn.getInfo()

    def get_type(self):
        return self.conn.getType()

    def get_xml(self, vmid):
        vm = self.conn.lookupByName(vmid)
        return vm.XMLDesc(0)

    def get_maxVcpus(self, vmid):
        vm = self.conn.lookupByName(vmid)
        return vm.maxVcpus()

    def get_maxMemory(self, vmid):
        vm = self.conn.lookupByName(vmid)
        return vm.maxMemory()

    def getFreeMemory(self):
        return self.conn.getFreeMemory()

    def get_autostart(self, vmid):
        vm = self.conn.lookupByName(vmid)
        return vm.autostart()

    def set_autostart(self, vmid, val):
        vm = self.conn.lookupByName(vmid)
        return vm.setAutostart(val)

    def define_from_xml(self, xml):
        return self.conn.defineXML(xml)


class Virt(object):

    def __init__(self, uri, module):
        self.module = module
        self.uri = uri

    def __get_conn(self):
        self.conn = LibvirtConnection(self.uri, self.module)
        return self.conn

    def get_vm(self, vmid):
        self.__get_conn()
        return self.conn.find_vm(vmid)

    def state(self):
        vms = self.list_vms()
        state = []
        for vm in vms:
            state_blurb = self.conn.get_status(vm)
            state.append("%s %s" % (vm, state_blurb))
        return state

    def info(self):
        vms = self.list_vms()
        info = dict()
        for vm in vms:
            data = self.conn.find_vm(vm).info()
            # libvirt returns maxMem, memory, and cpuTime as long()'s, which
            # xmlrpclib tries to convert to regular int's during serialization.
            # This throws exceptions, so convert them to strings here and
            # assume the other end of the xmlrpc connection can figure things
            # out or doesn't care.
            info[vm] = dict(
                state=VIRT_STATE_NAME_MAP.get(data[0], "unknown"),
                maxMem=str(data[1]),
                memory=str(data[2]),
                nrVirtCpu=data[3],
                cpuTime=str(data[4]),
                autostart=self.conn.get_autostart(vm),
            )

        return info

    def nodeinfo(self):
        self.__get_conn()
        data = self.conn.nodeinfo()
        info = dict(
            cpumodel=str(data[0]),
            phymemory=str(data[1]),
            cpus=str(data[2]),
            cpumhz=str(data[3]),
            numanodes=str(data[4]),
            sockets=str(data[5]),
            cpucores=str(data[6]),
            cputhreads=str(data[7])
        )
        return info

    def list_vms(self, state=None):
        self.conn = self.__get_conn()
        vms = self.conn.find_vm(-1)
        results = []
        for x in vms:
            try:
                if state:
                    vmstate = self.conn.get_status2(x)
                    if vmstate == state:
                        results.append(x.name())
                else:
                    results.append(x.name())
            except Exception:
                pass
        return results

    def virttype(self):
        return self.__get_conn().get_type()

    def autostart(self, vmid, as_flag):
        self.conn = self.__get_conn()
        # Change autostart flag only if needed
        if self.conn.get_autostart(vmid) != as_flag:
            self.conn.set_autostart(vmid, as_flag)
            return True

        return False

    def freemem(self):
        self.conn = self.__get_conn()
        return self.conn.getFreeMemory()

    def shutdown(self, vmid):
        """ Make the machine with the given vmid stop running.  Whatever that takes.  """
        self.__get_conn()
        self.conn.shutdown(vmid)
        return 0

    def pause(self, vmid):
        """ Pause the machine with the given vmid.  """

        self.__get_conn()
        return self.conn.suspend(vmid)

    def unpause(self, vmid):
        """ Unpause the machine with the given vmid.  """

        self.__get_conn()
        return self.conn.resume(vmid)

    def create(self, vmid):
        """ Start the machine via the given vmid """

        self.__get_conn()
        return self.conn.create(vmid)

    def start(self, vmid):
        """ Start the machine via the given id/name """

        self.__get_conn()
        return self.conn.create(vmid)

    def destroy(self, vmid):
        """ Pull the virtual power from the virtual domain, giving it virtually no time to virtually shut down.  """
        self.__get_conn()
        return self.conn.destroy(vmid)

    def undefine(self, vmid, flag):
        """ Stop a domain, and then wipe it from the face of the earth.  (delete disk/config file) """

        self.__get_conn()
        return self.conn.undefine(vmid, flag)

    def status(self, vmid):
        """
        Return a state suitable for server consumption.  Aka, codes.py values, not XM output.
        """
        self.__get_conn()
        return self.conn.get_status(vmid)

    def get_xml(self, vmid):
        """
        Receive a Vm id as input
        Return an xml describing vm config returned by a libvirt call
        """

        self.__get_conn()
        return self.conn.get_xml(vmid)

    def get_maxVcpus(self, vmid):
        """
        Gets the max number of VCPUs on a guest
        """

        self.__get_conn()
        return self.conn.get_maxVcpus(vmid)

    def get_max_memory(self, vmid):
        """
        Gets the max memory on a guest
        """

        self.__get_conn()
        return self.conn.get_MaxMemory(vmid)

    def define(self, xml):
        """
        Define a guest with the given xml
        """
        self.__get_conn()
        return self.conn.define_from_xml(xml)


def core(module):

    state = module.params.get('state', None)
    autostart = module.params.get('autostart', None)
    guest = module.params.get('name', None)
    command = module.params.get('command', None)
    force = module.params.get('force', None)
    flags = module.params.get('flags', None)
    uri = module.params.get('uri', None)
    xml = module.params.get('xml', None)

    v = Virt(uri, module)
    res = dict()

    if state and command == 'list_vms':
        res = v.list_vms(state=state)
        if not isinstance(res, dict):
            res = {command: res}
        return VIRT_SUCCESS, res

    if autostart is not None and command != 'define':
        if not guest:
            module.fail_json(msg="autostart requires 1 argument: name")
        try:
            v.get_vm(guest)
        except VMNotFound:
            module.fail_json(msg="domain %s not found" % guest)
        res['changed'] = v.autostart(guest, autostart)
        if not command and not state:
            return VIRT_SUCCESS, res

    if state:
        if not guest:
            module.fail_json(msg="state change requires a guest specified")

        if state == 'running':
            if v.status(guest) == 'paused':
                res['changed'] = True
                res['msg'] = v.unpause(guest)
            elif v.status(guest) != 'running':
                res['changed'] = True
                res['msg'] = v.start(guest)
        elif state == 'shutdown':
            if v.status(guest) != 'shutdown':
                res['changed'] = True
                res['msg'] = v.shutdown(guest)
        elif state == 'destroyed':
            if v.status(guest) != 'shutdown':
                res['changed'] = True
                res['msg'] = v.destroy(guest)
        elif state == 'paused':
            if v.status(guest) == 'running':
                res['changed'] = True
                res['msg'] = v.pause(guest)
        else:
            module.fail_json(msg="unexpected state")

        return VIRT_SUCCESS, res

    if command:
        if command in VM_COMMANDS:
            if command == 'define':
                if not xml:
                    module.fail_json(msg="define requires xml argument")
                if guest:
                    # there might be a mismatch between quest 'name' in the module and in the xml
                    module.warn("'xml' is given - ignoring 'name'")
                try:
                    domain_name = re.search('<name>(.*)</name>', xml).groups()[0]
                except AttributeError:
                    module.fail_json(msg="Could not find domain 'name' in xml")

                # From libvirt docs (https://libvirt.org/html/libvirt-libvirt-domain.html#virDomainDefineXML):
                # -- A previous definition for this domain would be overridden if it already exists.
                #
                # In real world testing with libvirt versions 1.2.17-13, 2.0.0-10 and 3.9.0-14
                # on qemu and lxc domains results in:
                # operation failed: domain '<name>' already exists with <uuid>
                #
                # In case a domain would be indeed overwritten, we should protect idempotency:
                try:
                    existing_domain_xml = v.get_vm(domain_name).XMLDesc(
                        libvirt.VIR_DOMAIN_XML_INACTIVE
                    )
                except VMNotFound:
                    existing_domain_xml = None
                try:
                    domain = v.define(xml)
                    if existing_domain_xml:
                        # if we are here, then libvirt redefined existing domain as the doc promised
                        if existing_domain_xml != domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE):
                            res = {'changed': True, 'change_reason': 'config changed'}
                    else:
                        res = {'changed': True, 'created': domain.name()}
                except libvirtError as e:
                    if e.get_error_code() != 9:  # 9 means 'domain already exists' error
                        module.fail_json(msg='libvirtError: %s' % e.get_error_message())
                if autostart is not None and v.autostart(domain_name, autostart):
                    res = {'changed': True, 'change_reason': 'autostart'}

            elif not guest:
                module.fail_json(msg="%s requires 1 argument: guest" % command)

            elif command == 'undefine':
                # Use the undefine function with flag to also handle various metadata.
                # This is especially important for UEFI enabled guests with nvram.
                # Provide flag as an integer of all desired bits, see 'ENTRY_UNDEFINE_FLAGS_MAP'.
                # Integer 23 takes care of all cases (23 = 1 + 2 + 4 + 16).
                flag = 0
                if flags is not None:
                    if force is True:
                        module.warn("Ignoring 'force', because 'flags' are provided.")
                    nv = ['nvram', 'keep_nvram']
                    # Check mutually exclusive flags
                    if set(nv) <= set(flags):
                        raise ValueError("Flags '%s' are mutually exclusive" % "' and '".join(nv))
                    for item in flags:
                        # Get and add flag integer from mapping, otherwise 0.
                        flag += ENTRY_UNDEFINE_FLAGS_MAP.get(item, 0)
                elif force is True:
                    flag = 23
                # Finally, execute with flag
                res = getattr(v, command)(guest, flag)
                if not isinstance(res, dict):
                    res = {command: res}

            else:
                res = getattr(v, command)(guest)
                if not isinstance(res, dict):
                    res = {command: res}

            return VIRT_SUCCESS, res

        elif hasattr(v, command):
            res = getattr(v, command)()
            if not isinstance(res, dict):
                res = {command: res}
            return VIRT_SUCCESS, res

        else:
            module.fail_json(msg="Command %s not recognized" % command)

    module.fail_json(msg="expected state or command parameter to be specified")


def main():
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type='str', aliases=['guest']),
            state=dict(type='str', choices=['destroyed', 'paused', 'running', 'shutdown']),
            autostart=dict(type='bool'),
            command=dict(type='str', choices=ALL_COMMANDS),
            flags=dict(type='list', elements='str', choices=ALL_FLAGS),
            force=dict(type='bool'),
            uri=dict(type='str', default='qemu:///system'),
            xml=dict(type='str'),
        ),
    )

    if not HAS_VIRT:
        module.fail_json(msg='The `libvirt` module is not importable. Check the requirements.')

    rc = VIRT_SUCCESS
    try:
        rc, result = core(module)
    except Exception as e:
        module.fail_json(msg=to_native(e), exception=traceback.format_exc())

    if rc != 0:  # something went wrong emit the msg
        module.fail_json(rc=rc, msg=result)
    else:
        module.exit_json(**result)


if __name__ == '__main__':
    main()
