# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import shutil
from itertools import chain
import subprocess

from charmhelpers.contrib.openstack.neutron import neutron_plugin_attribute
from copy import deepcopy

from charmhelpers.contrib.openstack import context, templating
from charmhelpers.contrib.openstack.utils import (
    git_clone_and_install,
    git_default_repos,
    git_generate_systemd_init_files,
    git_install_requested,
    git_pip_venv_dir,
    git_src_dir,
    pause_unit,
    resume_unit,
    make_assess_status_func,
    is_unit_paused_set,
    os_application_version_set,
    remote_restart,
    CompareOpenStackReleases,
    os_release,
)
from collections import OrderedDict
import neutron_ovs_context
from charmhelpers.contrib.network.ovs import (
    add_bridge,
    add_bridge_port,
    is_linuxbridge_interface,
    add_ovsbridge_linuxbridge,
    full_restart,
)
from charmhelpers.core.hookenv import (
    charm_dir,
    config,
    status_set,
    log,
)
from charmhelpers.contrib.openstack.neutron import (
    parse_bridge_mappings,
    determine_dkms_package,
    headers_package,
)
from charmhelpers.contrib.openstack.context import (
    ExternalPortContext,
    DataPortContext,
    WorkerConfigContext,
)
from charmhelpers.core.host import (
    adduser,
    add_group,
    add_user_to_group,
    lsb_release,
    mkdir,
    service,
    service_restart,
    service_running,
    write_file,
    CompareHostReleases,
)

from charmhelpers.core.templating import render

from charmhelpers.fetch import (
    apt_install,
    apt_purge,
    apt_update,
    filter_installed_packages,
)

from pci import PCINetDevices


# The interface is said to be satisfied if anyone of the interfaces in the
# list has a complete context.
# LY: Note the neutron-plugin is always present since that is the relation
#     with the principle and no data currently flows down from the principle
#     so there is no point in having it in REQUIRED_INTERFACES
REQUIRED_INTERFACES = {
    'messaging': ['amqp', 'zeromq-configuration'],
}

BASE_GIT_PACKAGES = [
    'ipset',
    'libffi-dev',
    'libssl-dev',
    'libxml2-dev',
    'libxslt1-dev',
    'libyaml-dev',
    'openstack-pkg-tools',
    'openvswitch-switch',
    'python-dev',
    'python-pip',
    'python-setuptools',
    'zlib1g-dev',
]

# ubuntu packages that should not be installed when deploying from git
GIT_PACKAGE_BLACKLIST = [
    'neutron-l3-agent',
    'neutron-metadata-agent',
    'neutron-server',
    'neutron-plugin-openvswitch',
    'neutron-plugin-openvswitch-agent',
    'neutron-openvswitch',
    'neutron-openvswitch-agent',
    'neutron-openvswitch-agent',
]

VERSION_PACKAGE = 'neutron-common'
NOVA_CONF_DIR = "/etc/nova"
NEUTRON_DHCP_AGENT_CONF = "/etc/neutron/dhcp_agent.ini"
NEUTRON_DNSMASQ_CONF = "/etc/neutron/dnsmasq.conf"
NEUTRON_CONF_DIR = "/etc/neutron"
NEUTRON_CONF = '%s/neutron.conf' % NEUTRON_CONF_DIR
NEUTRON_DEFAULT = '/etc/default/neutron-server'
NEUTRON_L3_AGENT_CONF = "/etc/neutron/l3_agent.ini"
NEUTRON_FWAAS_CONF = "/etc/neutron/fwaas_driver.ini"
ML2_CONF = '%s/plugins/ml2/ml2_conf.ini' % NEUTRON_CONF_DIR
OVS_CONF = '%s/plugins/ml2/openvswitch_agent.ini' % NEUTRON_CONF_DIR
EXT_PORT_CONF = '/etc/init/ext-port.conf'
NEUTRON_METADATA_AGENT_CONF = "/etc/neutron/metadata_agent.ini"
DVR_PACKAGES = ['neutron-l3-agent']
DHCP_PACKAGES = ['neutron-dhcp-agent']
METADATA_PACKAGES = ['neutron-metadata-agent']
PHY_NIC_MTU_CONF = '/etc/init/os-charm-phy-nic-mtu.conf'
TEMPLATES = 'templates/'
OVS_DEFAULT = '/etc/default/openvswitch-switch'
DPDK_INTERFACES = '/etc/dpdk/interfaces'
NEUTRON_SRIOV_AGENT_CONF = os.path.join(NEUTRON_CONF_DIR,
                                        'plugins/ml2/sriov_agent.ini')
NEUTRON_SRIOV_INIT_SCRIPT = os.path.join('/etc/init.d',
                                         'neutron-openvswitch-'
                                         'networking-sriov.sh')
NEUTRON_SRIOV_INIT_DEFAULT = os.path.join('/etc/default',
                                          'neutron-openvswitch-'
                                          'networking-sriov')
NEUTRON_SRIOV_SYSTEMD_UNIT = os.path.join('/lib/systemd/system',
                                          'neutron-openvswitch-'
                                          'networking-sriov.service')
NEUTRON_SRIOV_UPSTART_CONF = os.path.join('/etc/init',
                                          'neutron-openvswitch-'
                                          'networking-sriov.conf')

BASE_RESOURCE_MAP = OrderedDict([
    (NEUTRON_CONF, {
        'services': ['neutron-plugin-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext(),
                     neutron_ovs_context.RemoteRestartContext(
                         ['neutron-plugin', 'neutron-control']),
                     context.AMQPContext(ssl_dir=NEUTRON_CONF_DIR),
                     context.ZeroMQContext(),
                     context.NotificationDriverContext()],
    }),
    (ML2_CONF, {
        'services': ['neutron-plugin-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (OVS_CONF, {
        'services': ['neutron-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (OVS_DEFAULT, {
        'services': ['openvswitch-switch'],
        'contexts': [neutron_ovs_context.OVSDPDKDeviceContext(),
                     neutron_ovs_context.RemoteRestartContext(
                         ['neutron-plugin', 'neutron-control'])],
    }),
    (DPDK_INTERFACES, {
        'services': ['dpdk'],
        'contexts': [neutron_ovs_context.DPDKDeviceContext()],
    }),
    (PHY_NIC_MTU_CONF, {
        'services': ['os-charm-phy-nic-mtu'],
        'contexts': [context.PhyNICMTUContext()],
    }),
])
METADATA_RESOURCE_MAP = OrderedDict([
    (NEUTRON_METADATA_AGENT_CONF, {
        'services': ['neutron-metadata-agent'],
        'contexts': [neutron_ovs_context.SharedSecretContext(),
                     neutron_ovs_context.APIIdentityServiceContext(),
                     WorkerConfigContext()],
    }),
])
DHCP_RESOURCE_MAP = OrderedDict([
    (NEUTRON_DHCP_AGENT_CONF, {
        'services': ['neutron-dhcp-agent'],
        'contexts': [neutron_ovs_context.DHCPAgentContext()],
    }),
    (NEUTRON_DNSMASQ_CONF, {
        'services': ['neutron-dhcp-agent'],
        'contexts': [neutron_ovs_context.DHCPAgentContext()],
    }),
])
DVR_RESOURCE_MAP = OrderedDict([
    (NEUTRON_L3_AGENT_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [neutron_ovs_context.L3AgentContext()],
    }),
    (NEUTRON_FWAAS_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [neutron_ovs_context.L3AgentContext()],
    }),
    (EXT_PORT_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [context.ExternalPortContext()],
    }),
])
SRIOV_RESOURCE_MAP = OrderedDict([
    (NEUTRON_SRIOV_AGENT_CONF, {
        'services': ['neutron-sriov-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (NEUTRON_SRIOV_INIT_DEFAULT, {
        'services': [],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (NEUTRON_SRIOV_INIT_SCRIPT, {
        'services': [],
        'contexts': [],
    }),
    (NEUTRON_SRIOV_SYSTEMD_UNIT, {
        'services': [],
        'contexts': [],
    }),
    (NEUTRON_SRIOV_UPSTART_CONF, {
        'services': [],
        'contexts': [],
    }),
])

TEMPLATES = 'templates/'
INT_BRIDGE = "br-int"
EXT_BRIDGE = "br-ex"
DATA_BRIDGE = 'br-data'


def install_packages():
    status_set('maintenance', 'Installing apt packages')
    apt_update()
    # NOTE(jamespage): ensure early install of dkms related
    #                  dependencies for kernels which need
    #                  openvswitch via dkms (12.04).
    dkms_packages = determine_dkms_package()
    if dkms_packages:
        apt_install([headers_package()] + dkms_packages, fatal=True)
    apt_install(filter_installed_packages(determine_packages()),
                fatal=True)
    if use_dpdk():
        enable_ovs_dpdk()


def purge_packages(pkg_list):
    status_set('maintenance', 'Purging unused apt packages')
    purge_pkgs = []
    required_packages = determine_packages()
    for pkg in pkg_list:
        if pkg not in required_packages:
            purge_pkgs.append(pkg)
    if purge_pkgs:
        apt_purge(purge_pkgs, fatal=True)


def determine_packages():
    pkgs = []
    plugin_pkgs = neutron_plugin_attribute('ovs', 'packages', 'neutron')
    for plugin_pkg in plugin_pkgs:
        pkgs.extend(plugin_pkg)
    if use_dvr():
        pkgs.extend(DVR_PACKAGES)
    if enable_local_dhcp():
        pkgs.extend(DHCP_PACKAGES)
        pkgs.extend(METADATA_PACKAGES)

    if git_install_requested():
        pkgs.extend(BASE_GIT_PACKAGES)
        # don't include packages that will be installed from git
        for p in GIT_PACKAGE_BLACKLIST:
            if p in pkgs:
                pkgs.remove(p)

    cmp_release = CompareOpenStackReleases(
        os_release('neutron-common', base='icehouse'))
    if cmp_release >= 'mitaka' and 'neutron-plugin-openvswitch-agent' in pkgs:
        pkgs.remove('neutron-plugin-openvswitch-agent')
        pkgs.append('neutron-openvswitch-agent')

    if use_dpdk():
        pkgs.append('openvswitch-switch-dpdk')

    if enable_sriov():
        if cmp_release >= 'mitaka':
            pkgs.append('neutron-sriov-agent')
        else:
            pkgs.append('neutron-plugin-sriov-agent')

    return pkgs


def register_configs(release=None):
    release = release or os_release('neutron-common', base='icehouse')
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    for cfg, rscs in resource_map().items():
        configs.register(cfg, rscs['contexts'])
    return configs


def resource_map():
    '''
    Dynamically generate a map of resources that will be managed for a single
    hook execution.
    '''
    drop_config = []
    resource_map = deepcopy(BASE_RESOURCE_MAP)
    if use_dvr():
        resource_map.update(DVR_RESOURCE_MAP)
        resource_map.update(METADATA_RESOURCE_MAP)
        dvr_services = ['neutron-metadata-agent', 'neutron-l3-agent']
        resource_map[NEUTRON_CONF]['services'] += dvr_services
    if enable_local_dhcp():
        resource_map.update(METADATA_RESOURCE_MAP)
        resource_map.update(DHCP_RESOURCE_MAP)
        metadata_services = ['neutron-metadata-agent', 'neutron-dhcp-agent']
        resource_map[NEUTRON_CONF]['services'] += metadata_services
    # Remap any service names as required
    _os_release = os_release('neutron-common', base='icehouse')
    if CompareOpenStackReleases(_os_release) >= 'mitaka':
        # ml2_conf.ini -> openvswitch_agent.ini
        drop_config.append(ML2_CONF)
        # drop of -plugin from service name
        resource_map[NEUTRON_CONF]['services'].remove(
            'neutron-plugin-openvswitch-agent'
        )
        resource_map[NEUTRON_CONF]['services'].append(
            'neutron-openvswitch-agent'
        )
        if not use_dpdk():
            drop_config.append(DPDK_INTERFACES)
    else:
        drop_config.extend([OVS_CONF, DPDK_INTERFACES])

    if enable_sriov():
        sriov_agent_name = 'neutron-sriov-agent'
        sriov_resource_map = deepcopy(SRIOV_RESOURCE_MAP)

        if CompareOpenStackReleases(_os_release) < 'mitaka':
            sriov_agent_name = 'neutron-plugin-sriov-agent'
            # Patch resource_map for Kilo and Liberty
            sriov_resource_map[NEUTRON_SRIOV_AGENT_CONF]['services'] = \
                [sriov_agent_name]

        resource_map.update(sriov_resource_map)
        resource_map[NEUTRON_CONF]['services'].append(
            sriov_agent_name)

    # Use MAAS1.9 for MTU and external port config on xenial and above
    if CompareHostReleases(lsb_release()['DISTRIB_CODENAME']) >= 'xenial':
        drop_config.extend([EXT_PORT_CONF, PHY_NIC_MTU_CONF])

    for _conf in drop_config:
        try:
            del resource_map[_conf]
        except KeyError:
            pass

    return resource_map


def restart_map():
    '''
    Constructs a restart map based on charm config settings and relation
    state.
    '''
    return {k: v['services'] for k, v in resource_map().items()}


def get_topics():
    topics = []
    topics.append('q-agent-notifier-port-update')
    topics.append('q-agent-notifier-network-delete')
    topics.append('q-agent-notifier-tunnel-update')
    topics.append('q-agent-notifier-security_group-update')
    topics.append('q-agent-notifier-dvr-update')
    if context.NeutronAPIContext()()['l2_population']:
        topics.append('q-agent-notifier-l2population-update')
    return topics


def services():
    """Returns a list of (unique) services associate with this charm
    Note that we drop the os-charm-phy-nic-mtu service as it's not an actual
    running service that we can check for.

    @returns [strings] - list of service names suitable for (re)start_service()
    """
    s_set = set(chain(*restart_map().values()))
    s_set.discard('os-charm-phy-nic-mtu')
    return list(s_set)


def determine_ports():
    """Assemble a list of API ports for services the charm is managing

    @returns [ports] - list of ports that the charm manages.
    """
    ports = []
    if use_dvr():
        ports.append(DVR_RESOURCE_MAP[EXT_PORT_CONF]["ext_port"])
    return ports


UPDATE_ALTERNATIVES = ['update-alternatives', '--set', 'ovs-vswitchd']
OVS_DPDK_BIN = '/usr/lib/openvswitch-switch-dpdk/ovs-vswitchd-dpdk'
OVS_DEFAULT_BIN = '/usr/lib/openvswitch-switch/ovs-vswitchd'


def enable_ovs_dpdk():
    '''Enables the DPDK variant of ovs-vswitchd and restarts it'''
    subprocess.check_call(UPDATE_ALTERNATIVES + [OVS_DPDK_BIN])
    if not is_unit_paused_set():
        service_restart('openvswitch-switch')


def configure_ovs():
    status_set('maintenance', 'Configuring ovs')
    if not service_running('openvswitch-switch'):
        full_restart()
    datapath_type = determine_datapath_type()
    add_bridge(INT_BRIDGE, datapath_type)
    add_bridge(EXT_BRIDGE, datapath_type)
    ext_port_ctx = None
    if use_dvr():
        ext_port_ctx = ExternalPortContext()()
    if ext_port_ctx and ext_port_ctx['ext_port']:
        add_bridge_port(EXT_BRIDGE, ext_port_ctx['ext_port'])

    if not use_dpdk():
        portmaps = DataPortContext()()
        bridgemaps = parse_bridge_mappings(config('bridge-mappings'))
        for br in bridgemaps.values():
            add_bridge(br, datapath_type)
            if not portmaps:
                continue

            for port, _br in portmaps.items():
                if _br == br:
                    if not is_linuxbridge_interface(port):
                        add_bridge_port(br, port, promisc=True)
                    else:
                        add_ovsbridge_linuxbridge(br, port)
    else:
        # NOTE: when in dpdk mode, add based on pci bus order
        #       with type 'dpdk'
        dpdk_bridgemaps = neutron_ovs_context.resolve_dpdk_ports()
        device_index = 0
        for br in dpdk_bridgemaps.values():
            add_bridge(br, datapath_type)
            dpdk_add_bridge_port(br, 'dpdk{}'.format(device_index),
                                 port_type='dpdk')
            device_index += 1

    # Ensure this runs so that mtu is applied to data-port interfaces if
    # provided.
    # NOTE(ajkavanagh) for pause/resume we don't gate this as it's not a
    # running service, but rather running a few commands.
    service_restart('os-charm-phy-nic-mtu')


def configure_sriov():
    '''Configure SR-IOV devices based on provided configuration options

    NOTE(fnordahl): Boot time configuration is done by init script
    intalled by this charm.

    This function only does runtime configuration!
    '''
    charm_config = config()
    if not enable_sriov():
        return

    # make sure init script has correct mode and that boot time execution
    # is enabled
    os.chmod(NEUTRON_SRIOV_INIT_SCRIPT, 0o755)
    service('enable', 'neutron-openvswitch-networking-sriov')

    if charm_config.changed('sriov-numvfs'):
        devices = PCINetDevices()
        sriov_numvfs = charm_config.get('sriov-numvfs')

        # automatic configuration of all SR-IOV devices
        if sriov_numvfs == 'auto':
            log('Configuring SR-IOV device VF functions in auto mode')
            for device in devices.pci_devices:
                if device and device.sriov:
                    log("Configuring SR-IOV device"
                        " {} with {} VF's".format(device.interface_name,
                                                  device.sriov_totalvfs))
                    # NOTE(fnordahl): run-time change of numvfs is disallowed
                    # without resetting to 0 first.
                    device.set_sriov_numvfs(0)
                    device.set_sriov_numvfs(device.sriov_totalvfs)
        else:
            # Single int blanket configuration
            try:
                log('Configuring SR-IOV device VF functions'
                    ' with blanket setting')
                for device in devices.pci_devices:
                    if device and device.sriov:
                        numvfs = min(int(sriov_numvfs), device.sriov_totalvfs)
                        if int(sriov_numvfs) > device.sriov_totalvfs:
                            log('Requested value for sriov-numvfs ({}) too '
                                'high for interface {}. Falling back to '
                                'interface totalvfs '
                                'value: {}'.format(sriov_numvfs,
                                                   device.interface_name,
                                                   device.sriov_totalvfs))
                        log("Configuring SR-IOV device {} with {} "
                            "VFs".format(device.interface_name, numvfs))
                        # NOTE(fnordahl): run-time change of numvfs is
                        # disallowed without resetting to 0 first.
                        device.set_sriov_numvfs(0)
                        device.set_sriov_numvfs(numvfs)
            except ValueError:
                # <device>:<numvfs>[ <device>:numvfs] configuration
                sriov_numvfs = sriov_numvfs.split()
                for device_config in sriov_numvfs:
                    log('Configuring SR-IOV device VF functions per interface')
                    interface_name, numvfs = device_config.split(':')
                    device = devices.get_device_from_interface_name(
                        interface_name)
                    if device and device.sriov:
                        if int(numvfs) > device.sriov_totalvfs:
                            log('Requested value for sriov-numfs ({}) too '
                                'high for interface {}. Falling back to '
                                'interface totalvfs '
                                'value: {}'.format(numvfs,
                                                   device.interface_name,
                                                   device.sriov_totalvfs))
                            numvfs = device.sriov_totalvfs
                        log("Configuring SR-IOV device {} with {} "
                            "VF's".format(device.interface_name, numvfs))
                        # NOTE(fnordahl): run-time change of numvfs is
                        # disallowed without resetting to 0 first.
                        device.set_sriov_numvfs(0)
                        device.set_sriov_numvfs(int(numvfs))

        # Trigger remote restart in parent application
        remote_restart('neutron-plugin', 'nova-compute')

        # Restart of SRIOV agent is required after changes to system runtime
        # VF configuration
        cmp_release = CompareOpenStackReleases(
            os_release('neutron-common', base='icehouse'))
        if cmp_release >= 'mitaka':
            service_restart('neutron-sriov-agent')
        else:
            service_restart('neutron-plugin-sriov-agent')


def get_shared_secret():
    ctxt = neutron_ovs_context.SharedSecretContext()()
    if 'shared_secret' in ctxt:
        return ctxt['shared_secret']


def use_dvr():
    return context.NeutronAPIContext()()['enable_dvr']


def determine_datapath_type():
    '''
    Determine the ovs datapath type to use

    @returns string containing the datapath type
    '''
    if use_dpdk():
        return 'netdev'
    return 'system'


def use_dpdk():
    '''Determine whether DPDK should be used'''
    cmp_release = CompareOpenStackReleases(
        os_release('neutron-common', base='icehouse'))
    return (cmp_release >= 'mitaka' and config('enable-dpdk'))


def enable_sriov():
    '''Determine whether SR-IOV is enabled and supported'''
    cmp_release = CompareOpenStackReleases(
        os_release('neutron-common', base='icehouse'))
    return (cmp_release >= 'kilo' and config('enable-sriov'))


# TODO: update into charm-helpers to add port_type parameter
def dpdk_add_bridge_port(name, port, promisc=False, port_type=None):
    ''' Add a port to the named openvswitch bridge '''
    # log('Adding port {} to bridge {}'.format(port, name))
    cmd = ["ovs-vsctl", "--", "--may-exist", "add-port", name, port]
    if port_type:
        cmd += ['--', 'set', 'Interface', port,
                'type={}'.format(port_type)]
    subprocess.check_call(cmd)


def enable_nova_metadata():
    return use_dvr() or enable_local_dhcp()


def enable_local_dhcp():
    return config('enable-local-dhcp-and-metadata')


def git_install(projects_yaml):
    """Perform setup, and install git repos specified in yaml parameter."""
    status_set('maintenance', 'running git install')
    if git_install_requested():
        git_pre_install()
        projects_yaml = git_default_repos(projects_yaml)
        git_clone_and_install(projects_yaml, core_project='neutron')
        git_post_install(projects_yaml)


def git_pre_install():
    """Perform pre-install setup."""
    dirs = [
        '/var/lib/neutron',
        '/var/lib/neutron/lock',
        '/var/log/neutron',
    ]

    logs = [
        '/var/log/neutron/server.log',
    ]

    adduser('neutron', shell='/bin/bash', system_user=True)
    add_group('neutron', system_group=True)
    add_user_to_group('neutron', 'neutron')

    for d in dirs:
        mkdir(d, owner='neutron', group='neutron', perms=0o755, force=False)

    for l in logs:
        write_file(l, '', owner='neutron', group='neutron', perms=0o600)


def git_post_install(projects_yaml):
    """Perform post-install setup."""
    src_etc = os.path.join(git_src_dir(projects_yaml, 'neutron'), 'etc')
    configs = [
        {'src': src_etc,
         'dest': '/etc/neutron'},
        {'src': os.path.join(src_etc, 'neutron/plugins'),
         'dest': '/etc/neutron/plugins'},
        {'src': os.path.join(src_etc, 'neutron/rootwrap.d'),
         'dest': '/etc/neutron/rootwrap.d'},
    ]

    for c in configs:
        if os.path.exists(c['dest']):
            shutil.rmtree(c['dest'])
        shutil.copytree(c['src'], c['dest'])

    # NOTE(coreycb): Need to find better solution than bin symlinks.
    symlinks = [
        {'src': os.path.join(git_pip_venv_dir(projects_yaml),
                             'bin/neutron-rootwrap'),
         'link': '/usr/local/bin/neutron-rootwrap'},
    ]

    for s in symlinks:
        if os.path.lexists(s['link']):
            os.remove(s['link'])
        os.symlink(s['src'], s['link'])

    render('git/neutron_sudoers', '/etc/sudoers.d/neutron_sudoers', {},
           perms=0o440)

    bin_dir = os.path.join(git_pip_venv_dir(projects_yaml), 'bin')
    cmp_os_release = CompareOpenStackReleases(os_release('neutron-common'))
    # Use systemd init units/scripts from ubuntu wily onward
    _release = lsb_release()['DISTRIB_CODENAME']
    if CompareHostReleases(_release) >= 'wily':
        templates_dir = os.path.join(charm_dir(), 'templates/git')
        daemons = ['neutron-openvswitch-agent', 'neutron-ovs-cleanup']
        for daemon in daemons:
            neutron_ovs_context = {
                'daemon_path': os.path.join(bin_dir, daemon),
            }
            filename = daemon
            if daemon == 'neutron-openvswitch-agent':
                if cmp_os_release < 'mitaka':
                    filename = 'neutron-plugin-openvswitch-agent'
            template_file = 'git/{}.init.in.template'.format(filename)
            init_in_file = '{}.init.in'.format(filename)
            render(template_file, os.path.join(templates_dir, init_in_file),
                   neutron_ovs_context, perms=0o644)
        git_generate_systemd_init_files(templates_dir)

        for daemon in daemons:
            filename = daemon
            if daemon == 'neutron-openvswitch-agent':
                if cmp_os_release < 'mitaka':
                    filename = 'neutron-plugin-openvswitch-agent'
            service('enable', filename)
    else:
        neutron_ovs_agent_context = {
            'service_description': 'Neutron OpenvSwitch Plugin Agent',
            'charm_name': 'neutron-openvswitch',
            'process_name': 'neutron-openvswitch-agent',
            'executable_name': os.path.join(bin_dir,
                                            'neutron-openvswitch-agent'),
            'cleanup_process_name': 'neutron-ovs-cleanup',
            'plugin_config': '/etc/neutron/plugins/ml2/ml2_conf.ini',
            'log_file': '/var/log/neutron/openvswitch-agent.log',
        }

        neutron_ovs_cleanup_context = {
            'service_description': 'Neutron OpenvSwitch Cleanup',
            'charm_name': 'neutron-openvswitch',
            'process_name': 'neutron-ovs-cleanup',
            'executable_name': os.path.join(bin_dir, 'neutron-ovs-cleanup'),
            'log_file': '/var/log/neutron/ovs-cleanup.log',
        }

        if cmp_os_release < 'mitaka':
            render('git/upstart/neutron-plugin-openvswitch-agent.upstart',
                   '/etc/init/neutron-plugin-openvswitch-agent.conf',
                   neutron_ovs_agent_context, perms=0o644)
        else:
            render('git/upstart/neutron-plugin-openvswitch-agent.upstart',
                   '/etc/init/neutron-openvswitch-agent.conf',
                   neutron_ovs_agent_context, perms=0o644)
        render('git/upstart/neutron-ovs-cleanup.upstart',
               '/etc/init/neutron-ovs-cleanup.conf',
               neutron_ovs_cleanup_context, perms=0o644)

    if not is_unit_paused_set():
        service_restart('neutron-plugin-openvswitch-agent')


def assess_status(configs):
    """Assess status of current unit
    Decides what the state of the unit should be based on the current
    configuration.
    SIDE EFFECT: calls set_os_workload_status(...) which sets the workload
    status of the unit.
    Also calls status_set(...) directly if paused state isn't complete.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    assess_status_func(configs)()
    os_application_version_set(VERSION_PACKAGE)


def assess_status_func(configs):
    """Helper function to create the function that will assess_status() for
    the unit.
    Uses charmhelpers.contrib.openstack.utils.make_assess_status_func() to
    create the appropriate status function and then returns it.
    Used directly by assess_status() and also for pausing and resuming
    the unit.

    Note that required_interfaces is augmented with neutron-plugin-api if the
    nova_metadata is enabled.

    NOTE(ajkavanagh) ports are not checked due to race hazards with services
    that don't behave sychronously w.r.t their service scripts.  e.g.
    apache2.
    @param configs: a templating.OSConfigRenderer() object
    @return f() -> None : a function that assesses the unit's workload status
    """
    required_interfaces = REQUIRED_INTERFACES.copy()
    if enable_nova_metadata():
        required_interfaces['neutron-plugin-api'] = ['neutron-plugin-api']
    return make_assess_status_func(
        configs, required_interfaces,
        services=services(), ports=None)


def pause_unit_helper(configs):
    """Helper function to pause a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.pause_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(pause_unit, configs)


def resume_unit_helper(configs):
    """Helper function to resume a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.resume_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(resume_unit, configs)


def _pause_resume_helper(f, configs):
    """Helper function that uses the make_assess_status_func(...) from
    charmhelpers.contrib.openstack.utils to create an assess_status(...)
    function that can be used with the pause/resume of the unit
    @param f: the function to be used with the assess_status(...) function
    @returns None - this function is executed for its side-effect
    """
    # TODO(ajkavanagh) - ports= has been left off because of the race hazard
    # that exists due to service_start()
    f(assess_status_func(configs),
      services=services(),
      ports=None)
