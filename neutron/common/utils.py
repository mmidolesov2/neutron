# Copyright 2011, VMware, Inc.
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
#
# Borrowed from nova code base, more utilities will be added/borrowed as and
# when needed.

"""Utilities and helper functions."""

import functools
import importlib
import os
import os.path
import random
import re
import signal
import sys
import threading
import time
import uuid
import weakref

import eventlet
from eventlet.green import subprocess
import netaddr
from neutron_lib import constants as n_const
from neutron_lib.utils import helpers
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
from oslo_utils import excutils
import six

import neutron
from neutron._i18n import _
from neutron.conf import common as common_config
from neutron.db import api as db_api

try:
    # This isn't available on all platforms (e.g. Windows).
    import resource
except ImportError:
    resource = None


TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LOG = logging.getLogger(__name__)

DEFAULT_THROTTLER_VALUE = 2

_SEPARATOR_REGEX = re.compile(r'[/\\]+')


class WaitTimeout(Exception):
    """Default exception coming from wait_until_true() function."""


class LockWithTimer(object):
    def __init__(self, threshold):
        self._threshold = threshold
        self.timestamp = 0
        self._lock = threading.Lock()

    def acquire(self):
        return self._lock.acquire(False)

    def release(self):
        return self._lock.release()

    def time_to_wait(self):
        return self.timestamp - time.time() + self._threshold


# REVISIT(jlibosva): Some parts of throttler may be similar to what
#                    neutron.notifiers.batch_notifier.BatchNotifier does. They
#                    could be refactored and unified.
def throttler(threshold=DEFAULT_THROTTLER_VALUE):
    """Throttle number of calls to a function to only once per 'threshold'.
    """
    def decorator(f):
        lock_with_timer = LockWithTimer(threshold)

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if lock_with_timer.acquire():
                try:
                    fname = f.__name__
                    time_to_wait = lock_with_timer.time_to_wait()
                    if time_to_wait > 0:
                        LOG.debug("Call of function %s scheduled, sleeping "
                                  "%.1f seconds", fname, time_to_wait)
                        # Decorated function has been called recently, wait.
                        eventlet.sleep(time_to_wait)
                    lock_with_timer.timestamp = time.time()
                finally:
                    lock_with_timer.release()
                LOG.debug("Calling throttled function %s", fname)
                return f(*args, **kwargs)
        return wrapper
    return decorator


def _subprocess_setup():
    # Python installs a SIGPIPE handler by default. This is usually not what
    # non-Python subprocesses expect.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def subprocess_popen(args, stdin=None, stdout=None, stderr=None, shell=False,
                     env=None, preexec_fn=_subprocess_setup, close_fds=True):

    # Set sensible FD limits - this is an adaption from oslo.rootwrap
    # See https://github.com/openstack/oslo.rootwrap/commit/c0a86998203315858721a7b2c8ab75fbf5cd51d9
    if not getattr(subprocess_popen, '_ccloud_fd_patch', False) and resource:
        # When use close_fds=True on Python 2.x, we spend significant time
        # in closing fds up to current soft ulimit, which could be large.
        # Lower our ulimit to a reasonable value to regain performance.
        fd_limits = resource.getrlimit(resource.RLIMIT_NOFILE)
        # sensible_fd_limit = min(common_config.rlimit_nofile, fd_limits[0])
        sensible_fd_limit = min(1024, fd_limits[0])
        if (fd_limits[0] > sensible_fd_limit):
            # Unfortunately this inherits to our children, so allow them to
            # re-raise by passing through the hard limit unmodified
            resource.setrlimit(
                resource.RLIMIT_NOFILE, (sensible_fd_limit, fd_limits[1]))
            # This is set on import to the hard ulimit. if its defined we
            # already have imported it, so we need to update it to the new
            # limit.
            if (hasattr(subprocess, 'MAXFD') and
                    subprocess.MAXFD > sensible_fd_limit):
                subprocess.MAXFD = sensible_fd_limit
                subprocess_popen._ccloud_fd_patch = True
        else:
            subprocess_popen._ccloud_fd_patch = True

    return subprocess.Popen(args, shell=shell, stdin=stdin, stdout=stdout,
                            stderr=stderr, preexec_fn=preexec_fn,
                            close_fds=close_fds, env=env)


def get_first_host_ip(net, ip_version):
    return str(netaddr.IPAddress(net.first + 1, ip_version))


def is_extension_supported(plugin, ext_alias):
    return ext_alias in getattr(
        plugin, "supported_extension_aliases", [])


def log_opt_values(log):
    cfg.CONF.log_opt_values(log, logging.DEBUG)


def get_dhcp_agent_device_id(network_id, host):
    # Split host so as to always use only the hostname and
    # not the domain name. This will guarantee consistency
    # whether a local hostname or an fqdn is passed in.
    local_hostname = host.split('.')[0]
    host_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, str(local_hostname))
    return 'dhcp%s-%s' % (host_uuid, network_id)


class exception_logger(object):
    """Wrap a function and log raised exception

    :param logger: the logger to log the exception default is LOG.exception

    :returns: origin value if no exception raised; re-raise the exception if
              any occurred

    """
    def __init__(self, logger=None):
        self.logger = logger

    def __call__(self, func):
        if self.logger is None:
            LOG = logging.getLogger(func.__module__)
            self.logger = LOG.exception

        def call(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                with excutils.save_and_reraise_exception():
                    self.logger(e)
        return call


def get_other_dvr_serviced_device_owners():
    """Return device_owner names for ports that should be serviced by DVR

    This doesn't return DEVICE_OWNER_COMPUTE_PREFIX since it is a
    prefix, not a complete device_owner name, so should be handled
    separately (see is_dvr_serviced() below)
    """
    return [n_const.DEVICE_OWNER_LOADBALANCER,
            n_const.DEVICE_OWNER_LOADBALANCERV2,
            n_const.DEVICE_OWNER_DHCP]


def get_dvr_allowed_address_pair_device_owners():
    """Return device_owner names for allowed_addr_pair ports serviced by DVR

    This just returns the device owners that are used by the
    allowed_address_pair ports. Right now only the device_owners shown
    below are used by the allowed_address_pair ports.
    Later if other device owners are used for allowed_address_pairs those
    device_owners should be added to the list below.
    """
    # TODO(Swami): Convert these methods to constants.
    # Add the constants variable to the neutron-lib
    return [n_const.DEVICE_OWNER_LOADBALANCER,
            n_const.DEVICE_OWNER_LOADBALANCERV2]


def is_dvr_serviced(device_owner):
    """Check if the port need to be serviced by DVR

    Helper function to check the device owners of the
    ports in the compute and service node to make sure
    if they are required for DVR or any service directly or
    indirectly associated with DVR.
    """
    return (device_owner.startswith(n_const.DEVICE_OWNER_COMPUTE_PREFIX) or
            device_owner in get_other_dvr_serviced_device_owners())


def is_fip_serviced(device_owner):
    """Check if the port can be assigned a floating IP

    Helper function to check the device owner of a
    port can be assigned a floating IP.
    """
    return device_owner != n_const.DEVICE_OWNER_DHCP


def ip_to_cidr(ip, prefix=None):
    """Convert an ip with no prefix to cidr notation

    :param ip: An ipv4 or ipv6 address.  Convertable to netaddr.IPNetwork.
    :param prefix: Optional prefix.  If None, the default 32 will be used for
        ipv4 and 128 for ipv6.
    """
    net = netaddr.IPNetwork(ip)
    if prefix is not None:
        # Can't pass ip and prefix separately.  Must concatenate strings.
        net = netaddr.IPNetwork(str(net.ip) + '/' + str(prefix))
    return str(net)


def cidr_to_ip(ip_cidr):
    """Strip the cidr notation from an ip cidr or ip

    :param ip_cidr: An ipv4 or ipv6 address, with or without cidr notation
    """
    net = netaddr.IPNetwork(ip_cidr)
    return str(net.ip)


def fixed_ip_cidrs(fixed_ips):
    """Create a list of a port's fixed IPs in cidr notation.

    :param fixed_ips: A neutron port's fixed_ips dictionary
    """
    return [ip_to_cidr(fixed_ip['ip_address'], fixed_ip.get('prefixlen'))
            for fixed_ip in fixed_ips]


def is_cidr_host(cidr):
    """Determines if the cidr passed in represents a single host network

    :param cidr: Either an ipv4 or ipv6 cidr.
    :returns: True if the cidr is /32 for ipv4 or /128 for ipv6.
    :raises ValueError: raises if cidr does not contain a '/'.  This disallows
        plain IP addresses specifically to avoid ambiguity.
    """
    if '/' not in str(cidr):
        raise ValueError(_("cidr doesn't contain a '/'"))
    net = netaddr.IPNetwork(cidr)
    if net.version == 4:
        return net.prefixlen == n_const.IPv4_BITS
    return net.prefixlen == n_const.IPv6_BITS


def get_ip_version(ip_or_cidr):
    return netaddr.IPNetwork(ip_or_cidr).version


def ip_version_from_int(ip_version_int):
    if ip_version_int == 4:
        return n_const.IPv4
    if ip_version_int == 6:
        return n_const.IPv6
    raise ValueError(_('Illegal IP version number'))


class DelayedStringRenderer(object):
    """Takes a callable and its args and calls when __str__ is called

    Useful for when an argument to a logging statement is expensive to
    create. This will prevent the callable from being called if it's
    never converted to a string.
    """

    def __init__(self, function, *args, **kwargs):
        self.function = function
        self.args = args
        self.kwargs = kwargs

    def __str__(self):
        return str(self.function(*self.args, **self.kwargs))


def _hex_format(port, mask=0):

    def hex_str(num):
        return format(num, '#06x')
    if mask > 0:
        return "%s/%s" % (hex_str(port), hex_str(0xffff & ~mask))
    return hex_str(port)


def _gen_rules_port_min(port_min, top_bit):
    """
    Encode a port range range(port_min, (port_min | (top_bit - 1)) + 1) into
    a set of bit value/masks.
    """
    # Processing starts with setting up mask and top_bit variables to their
    # maximum. Top_bit has the form (1000000) with '1' pointing to the register
    # being processed, while mask has the form (0111111) with '1' showing
    # possible range to be covered.

    # With each rule generation cycle, mask and top_bit are bit shifted to the
    # right. When top_bit reaches 0 it means that last register was processed.

    # Let port_min be n bits long, top_bit = 1 << k, 0<=k<=n-1.

    # Each cycle step checks the following conditions:

    #     1). port & mask == 0
    #     This means that remaining bits k..1 are equal to '0' and can be
    #     covered by a single port/mask rule.

    #     If condition 1 doesn't fit, then both top_bit and mask are bit
    #     shifted to the right and condition 2 is checked:

    #     2). port & top_bit == 0
    #     This means that kth port bit is equal to '0'. By setting it to '1'
    #     and masking other (k-1) bits all ports in range
    #     [P, P + 2^(k-1)-1] are guaranteed to be covered.
    #     Let p_k be equal to port first (n-k) bits with rest set to 0.
    #     Then P = p_k | top_bit.

    # Correctness proof:
    # The remaining range to be encoded in a cycle is calculated as follows:
    # R = [port_min, port_min | mask].
    # If condition 1 holds, then a rule that covers R is generated and the job
    # is done.
    # If condition 2 holds, then the rule emitted will cover 2^(k-1) values
    # from the range. Remaining range R will shrink by 2^(k-1).
    # If condition 2 doesn't hold, then even after top_bit/mask shift in next
    # iteration the value of R won't change.

    # Full cycle example for range [40, 64):
    # port=0101000, top_bit=1000000, k=6
    # * step 1, k=6, R=[40, 63]
    #   top_bit=1000000, mask=0111111 -> condition 1 doesn't hold, shifting
    #                                    mask/top_bit
    #   top_bit=0100000, mask=0011111 -> condition 2 doesn't hold

    # * step 2, k=5, R=[40, 63]
    #   top_bit=0100000, mask=0011111 -> condition 1 doesn't hold, shifting
    #                                    mask/top_bit
    #   top_bit=0010000, mask=0001111 -> condition 2 holds -> 011xxxx or
    #                                                         0x0030/fff0
    # * step 3, k=4, R=[40, 47]
    #   top_bit=0010000, mask=0001111 -> condition 1 doesn't hold, shifting
    #                                    mask/top_bit
    #   top_bit=0001000, mask=0000111 -> condition 2 doesn't hold

    # * step 4, k=3, R=[40, 47]
    #   top_bit=0001000, mask=0000111 -> condition 1 holds -> 0101xxx or
    #                                                         0x0028/fff8

    #   rules=[0x0030/fff0, 0x0028/fff8]

    rules = []
    mask = top_bit - 1

    while True:
        if (port_min & mask) == 0:
            # greedy matched a streak of '0' in port_min
            rules.append(_hex_format(port_min, mask))
            break
        top_bit >>= 1
        mask >>= 1
        if (port_min & top_bit) == 0:
            # matched next '0' in port_min to substitute for '1' in resulting
            # rule
            rules.append(_hex_format(port_min & ~mask | top_bit, mask))
    return rules


def _gen_rules_port_max(port_max, top_bit):
    """
    Encode a port range range(port_max & ~(top_bit - 1), port_max + 1) into
    a set of bit value/masks.
    """
    # Processing starts with setting up mask and top_bit variables to their
    # maximum. Top_bit has the form (1000000) with '1' pointing to the register
    # being processed, while mask has the form (0111111) with '1' showing
    # possible range to be covered.

    # With each rule generation cycle, mask and top_bit are bit shifted to the
    # right. When top_bit reaches 0 it means that last register was processed.

    # Let port_max be n bits long, top_bit = 1 << k, 0<=k<=n-1.

    # Each cycle step checks the following conditions:

    #     1). port & mask == mask
    #     This means that remaining bits k..1 are equal to '1' and can be
    #     covered by a single port/mask rule.

    #     If condition 1 doesn't fit, then both top_bit and mask are bit
    #     shifted to the right and condition 2 is checked:

    #     2). port & top_bit == top_bit
    #     This means that kth port bit is equal to '1'. By setting it to '0'
    #     and masking other (k-1) bits all ports in range
    #     [P, P + 2^(k-1)-1] are guaranteed to be covered.
    #     Let p_k be equal to port first (n-k) bits with rest set to 0.
    #     Then P = p_k | ~top_bit.

    # Correctness proof:
    # The remaining range to be encoded in a cycle is calculated as follows:
    # R = [port_max & ~mask, port_max].
    # If condition 1 holds, then a rule that covers R is generated and the job
    # is done.
    # If condition 2 holds, then the rule emitted will cover 2^(k-1) values
    # from the range. Remaining range R will shrink by 2^(k-1).
    # If condition 2 doesn't hold, then even after top_bit/mask shift in next
    # iteration the value of R won't change.

    # Full cycle example for range [64, 105]:
    # port=1101001, top_bit=1000000, k=6
    # * step 1, k=6, R=[64, 105]
    #   top_bit=1000000, mask=0111111 -> condition 1 doesn't hold, shifting
    #                                    mask/top_bit
    #   top_bit=0100000, mask=0011111 -> condition 2 holds -> 10xxxxx or
    #                                                         0x0040/ffe0
    # * step 2, k=5, R=[96, 105]
    #   top_bit=0100000, mask=0011111 -> condition 1 doesn't hold, shifting
    #                                    mask/top_bit
    #   top_bit=0010000, mask=0001111 -> condition 2 doesn't hold

    # * step 3, k=4, R=[96, 105]
    #   top_bit=0010000, mask=0001111 -> condition 1 doesn't hold, shifting
    #                                    mask/top_bit
    #   top_bit=0001000, mask=0000111 -> condition 2 holds -> 1100xxx or
    #                                                         0x0060/fff8
    # * step 4, k=3, R=[104, 105]
    #   top_bit=0001000, mask=0000111 -> condition 1 doesn't hold, shifting
    #                                    mask/top_bit
    #   top_bit=0000100, mask=0000011 -> condition 2 doesn't hold

    # * step 5, k=2, R=[104, 105]
    #   top_bit=0000100, mask=0000011 -> condition 1 doesn't hold, shifting
    #                                    mask/top_bit
    #   top_bit=0000010, mask=0000001 -> condition 2 doesn't hold

    # * step 6, k=1, R=[104, 105]
    #   top_bit=0000010, mask=0000001 -> condition 1 holds -> 1101001 or
    #                                                         0x0068

    #   rules=[0x0040/ffe0, 0x0060/fff8, 0x0068]

    rules = []
    mask = top_bit - 1

    while True:
        if (port_max & mask) == mask:
            # greedy matched a streak of '1' in port_max
            rules.append(_hex_format(port_max & ~mask, mask))
            break
        top_bit >>= 1
        mask >>= 1
        if (port_max & top_bit) == top_bit:
            # matched next '1' in port_max to substitute for '0' in resulting
            # rule
            rules.append(_hex_format(port_max & ~mask & ~top_bit, mask))
    return rules


def port_rule_masking(port_min, port_max):
    """Translate a range [port_min, port_max] into a set of bitwise matches.

    Each match has the form 'port/mask'. The port and mask are 16-bit numbers
    written in hexadecimal prefixed by 0x. Each 1-bit in mask requires that
    the corresponding bit in port must match. Each 0-bit in mask causes the
    corresponding bit to be ignored.
    """

    # Let binary representation of port_min and port_max be n bits long and
    # have first m bits in common, 0 <= m <= n.

    # If remaining (n - m) bits of given ports define 2^(n-m) values, then
    # [port_min, port_max] range is covered by a single rule.
    # For example:
    # n = 6
    # port_min = 16 (binary 010000)
    # port_max = 23 (binary 010111)
    # Ports have m=3 bits in common with the remaining (n-m)=3 bits
    # covering range [0, 2^3), which equals to a single 010xxx rule. The algo
    # will return [0x0010/fff8].

    # Else [port_min, port_max] range will be split into 2: range [port_min, T)
    # and [T, port_max]. Let p_m be the common part of port_min and port_max
    # with other (n-m) bits set to 0. Then T = p_m | 1 << (n-m-1).
    # For example:
    # n = 7
    # port_min = 40  (binary 0101000)
    # port_max = 105 (binary 1101001)
    # Ports have m=0 bits in common, p_m=000000. Then T=1000000 and the
    # initial range [40, 105] is divided into [40, 64) and [64, 105].
    # Each of the ranges will be processed separately, then the generated rules
    # will be merged.

    # Check port_max >= port_min.
    if port_max < port_min:
        raise ValueError(_("'port_max' is smaller than 'port_min'"))

    bitdiff = port_min ^ port_max
    if bitdiff == 0:
        # port_min == port_max
        return [_hex_format(port_min)]
    # for python3.x, bit_length could be used here
    top_bit = 1
    while top_bit <= bitdiff:
        top_bit <<= 1
    if (port_min & (top_bit - 1) == 0 and
            port_max & (top_bit - 1) == top_bit - 1):
        # special case, range of 2^k ports is covered
        return [_hex_format(port_min, top_bit - 1)]

    top_bit >>= 1
    rules = []
    rules.extend(_gen_rules_port_min(port_min, top_bit))
    rules.extend(_gen_rules_port_max(port_max, top_bit))
    return rules


def create_object_with_dependency(creator, dep_getter, dep_creator,
                                  dep_id_attr, dep_deleter):
    """Creates an object that binds to a dependency while handling races.

    creator is a function that expected to take the result of either
    dep_getter or dep_creator.

    The result of dep_getter and dep_creator must have an attribute of
    dep_id_attr be used to determine if the dependency changed during object
    creation.

    dep_deleter will be called with a the result of dep_creator if the creator
    function fails due to a non-dependency reason or the retries are exceeded.

    dep_getter should return None if the dependency does not exist.

    dep_creator can raise a DBDuplicateEntry to indicate that a concurrent
    create of the dependency occurred and the process will restart to get the
    concurrently created one.

    This function will return both the created object and the dependency it
    used/created.

    This function protects against all of the cases where the dependency can
    be concurrently removed by catching exceptions and restarting the
    process of creating the dependency if one no longer exists. It will
    give up after neutron.db.api.MAX_RETRIES and raise the exception it
    encounters after that.
    """
    result, dependency, dep_id, made_locally = None, None, None, False
    for attempts in range(1, db_api.MAX_RETRIES + 1):
        # we go to max + 1 here so the exception handlers can raise their
        # errors at the end
        try:
            dependency = dep_getter()
            if not dependency:
                dependency = dep_creator()
                made_locally = True
            dep_id = getattr(dependency, dep_id_attr)
        except db_exc.DBDuplicateEntry:
            # dependency was concurrently created.
            with excutils.save_and_reraise_exception() as ctx:
                if attempts < db_api.MAX_RETRIES:
                    # sleep for a random time between 0 and 1 second to
                    # make sure a concurrent worker doesn't retry again
                    # at exactly the same time
                    time.sleep(random.uniform(0, 1))
                    ctx.reraise = False
                    continue
        try:
            result = creator(dependency)
            break
        except Exception:
            with excutils.save_and_reraise_exception() as ctx:
                # check if dependency we tried to use was removed during
                # object creation
                if attempts < db_api.MAX_RETRIES:
                    dependency = dep_getter()
                    if not dependency or dep_id != getattr(dependency,
                                                           dep_id_attr):
                        ctx.reraise = False
                        continue
                # we have exceeded retries or have encountered a non-dependency
                # related failure so we try to clean up the dependency if we
                # created it before re-raising
                if made_locally and dependency:
                    try:
                        dep_deleter(dependency)
                    except Exception:
                        LOG.exception("Failed cleaning up dependency %s",
                                      dep_id)
    return result, dependency


def transaction_guard(f):
    """Ensures that the context passed in is not in a transaction.

    Various Neutron methods modifying resources have assumptions that they will
    not be called inside of a transaction because they perform operations that
    expect all data to be committed to the database (e.g. ML2 postcommit calls)
    and/or they have side effects on external systems.
    So calling them in a transaction can lead to consistency errors on failures
    since the side effect will not be reverted on a DB rollback.

    If you receive this error, you must alter your code to handle the fact that
    the thing you are calling can have side effects so using transactions to
    undo on failures is not possible.
    """
    @functools.wraps(f)
    def inner(self, context, *args, **kwargs):
        # FIXME(kevinbenton): get rid of all uses of this flag
        if (context.session.is_active and
                getattr(context, 'GUARD_TRANSACTION', True)):
            raise RuntimeError(_("Method %s cannot be called within a "
                                 "transaction.") % f)
        return f(self, context, *args, **kwargs)
    return inner


def wait_until_true(predicate, timeout=60, sleep=1, exception=None):
    """
    Wait until callable predicate is evaluated as True

    :param predicate: Callable deciding whether waiting should continue.
    Best practice is to instantiate predicate with functools.partial()
    :param timeout: Timeout in seconds how long should function wait.
    :param sleep: Polling interval for results in seconds.
    :param exception: Exception instance to raise on timeout. If None is passed
                      (default) then WaitTimeout exception is raised.
    """
    try:
        with eventlet.Timeout(timeout):
            while not predicate():
                eventlet.sleep(sleep)
    except eventlet.Timeout:
        if exception is not None:
            #pylint: disable=raising-bad-type
            raise exception
        raise WaitTimeout("Timed out after %d seconds" % timeout)


class _AuthenticBase(object):
    def __init__(self, addr, **kwargs):
        super(_AuthenticBase, self).__init__(addr, **kwargs)
        self._initial_value = addr

    def __str__(self):
        if isinstance(self._initial_value, six.string_types):
            return self._initial_value
        return super(_AuthenticBase, self).__str__()

    # NOTE(ihrachys): override deepcopy because netaddr.* classes are
    # slot-based and hence would not copy _initial_value
    def __deepcopy__(self, memo):
        return self.__class__(self._initial_value)


class AuthenticEUI(_AuthenticBase, netaddr.EUI):
    '''
    This class retains the format of the MAC address string passed during
    initialization.

    This is useful when we want to make sure that we retain the format passed
    by a user through API.
    '''


class AuthenticIPNetwork(_AuthenticBase, netaddr.IPNetwork):
    '''
    This class retains the format of the IP network string passed during
    initialization.

    This is useful when we want to make sure that we retain the format passed
    by a user through API.
    '''


class classproperty(object):
    def __init__(self, f):
        self.func = f

    def __get__(self, obj, owner):
        return self.func(owner)


_NO_ARGS_MARKER = object()


def attach_exc_details(e, msg, args=_NO_ARGS_MARKER):
    e._error_context_msg = msg
    e._error_context_args = args


def extract_exc_details(e):
    for attr in ('_error_context_msg', '_error_context_args'):
        if not hasattr(e, attr):
            return u'No details.'
    details = e._error_context_msg
    args = e._error_context_args
    if args is _NO_ARGS_MARKER:
        return details
    return details % args


def import_modules_recursively(topdir):
    '''Import and return all modules below the topdir directory.'''
    topdir = _SEPARATOR_REGEX.sub('/', topdir)
    modules = []
    for root, dirs, files in os.walk(topdir):
        for file_ in files:
            if file_[-3:] != '.py':
                continue

            module = file_[:-3]
            if module == '__init__':
                continue

            import_base = _SEPARATOR_REGEX.sub('.', root)

            # NOTE(ihrachys): in Python3, or when we are not located in the
            # directory containing neutron code, __file__ is absolute, so we
            # should truncate it to exclude PYTHONPATH prefix
            prefixlen = len(os.path.dirname(neutron.__file__))
            import_base = 'neutron' + import_base[prefixlen:]

            module = '.'.join([import_base, module])
            if module not in sys.modules:
                importlib.import_module(module)
            modules.append(module)

    return modules


def get_rand_name(max_length=None, prefix='test'):
    """Return a random string.

    The string will start with 'prefix' and will be exactly 'max_length'.
    If 'max_length' is None, then exactly 8 random characters, each
    hexadecimal, will be added. In case len(prefix) <= len(max_length),
    ValueError will be raised to indicate the problem.
    """
    return get_related_rand_names([prefix], max_length)[0]


def get_rand_device_name(prefix='test'):
    return get_rand_name(
        max_length=n_const.DEVICE_NAME_MAX_LEN, prefix=prefix)


def get_related_rand_names(prefixes, max_length=None):
    """Returns a list of the prefixes with the same random characters appended

    :param prefixes: A list of prefix strings
    :param max_length: The maximum length of each returned string
    :returns: A list with each prefix appended with the same random characters
    """

    if max_length:
        length = max_length - max(len(p) for p in prefixes)
        if length <= 0:
            raise ValueError(
                _("'max_length' must be longer than all prefixes"))
    else:
        length = 8
    rndchrs = helpers.get_random_string(length)
    return [p + rndchrs for p in prefixes]


def get_related_rand_device_names(prefixes):
    return get_related_rand_names(prefixes,
                                  max_length=n_const.DEVICE_NAME_MAX_LEN)


try:
    # PY3
    weak_method = weakref.WeakMethod
except AttributeError:
    # PY2
    import weakrefmethod
    weak_method = weakrefmethod.WeakMethod


def make_weak_ref(f):
    """Make a weak reference to a function accounting for bound methods."""
    return weak_method(f) if hasattr(f, '__self__') else weakref.ref(f)


def resolve_ref(ref):
    """Handles dereference of weakref."""
    if isinstance(ref, weakref.ref):
        ref = ref()
    return ref


def bytes_to_bits(value):
    return value * 8


def bits_to_kilobits(value, base):
    #NOTE(slaweq): round up that even 1 bit will give 1 kbit as a result
    return int((value + (base - 1)) / base)
