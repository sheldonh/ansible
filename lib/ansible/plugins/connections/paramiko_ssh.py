# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# ---
# The paramiko transport is provided because many distributions, in particular EL6 and before
# do not support ControlPersist in their SSH implementations.  This is needed on the Ansible
# control machine to be reasonably efficient with connections.  Thus paramiko is faster
# for most users on these platforms.  Users with ControlPersist capability can consider
# using -c ssh or configuring the transport in ansible.cfg.

import warnings
import os
import pipes
import socket
import random
import logging
import tempfile
import traceback
import fcntl
import re
import sys

from termios import tcflush, TCIFLUSH
from binascii import hexlify

from ansible import constants as C
from ansible.errors import AnsibleError, AnsibleConnectionFailure, AnsibleFileNotFound
from ansible.plugins.connections import ConnectionBase
from ansible.utils.path import makedirs_safe
from ansible.utils.debug import debug

AUTHENTICITY_MSG="""
paramiko: The authenticity of host '%s' can't be established.
The %s key fingerprint is %s.
Are you sure you want to continue connecting (yes/no)?
"""

# prevent paramiko warning noise -- see http://stackoverflow.com/questions/3920502/
HAVE_PARAMIKO=False
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        import paramiko
        HAVE_PARAMIKO=True
        logging.getLogger("paramiko").setLevel(logging.WARNING)
    except ImportError:
        pass


class MyAddPolicy(object):
    """
    Based on AutoAddPolicy in paramiko so we can determine when keys are added
    and also prompt for input.

    Policy for automatically adding the hostname and new host key to the
    local L{HostKeys} object, and saving it.  This is used by L{SSHClient}.
    """

    def __init__(self, new_stdin):
        self._new_stdin = new_stdin

    def missing_host_key(self, client, hostname, key):

        if C.HOST_KEY_CHECKING:

            # FIXME: need to fix lock file stuff
            #fcntl.lockf(self.runner.process_lockfile, fcntl.LOCK_EX)
            #fcntl.lockf(self.runner.output_lockfile, fcntl.LOCK_EX)

            old_stdin = sys.stdin
            sys.stdin = self._new_stdin

            # clear out any premature input on sys.stdin
            tcflush(sys.stdin, TCIFLUSH)

            fingerprint = hexlify(key.get_fingerprint())
            ktype = key.get_name()

            inp = raw_input(AUTHENTICITY_MSG % (hostname, ktype, fingerprint))
            sys.stdin = old_stdin

            if inp not in ['yes','y','']:
                # FIXME: lock file stuff
                #fcntl.flock(self.runner.output_lockfile, fcntl.LOCK_UN)
                #fcntl.flock(self.runner.process_lockfile, fcntl.LOCK_UN)
                raise AnsibleError("host connection rejected by user")

            # FIXME: lock file stuff
            #fcntl.lockf(self.runner.output_lockfile, fcntl.LOCK_UN)
            #fcntl.lockf(self.runner.process_lockfile, fcntl.LOCK_UN)


        key._added_by_ansible_this_time = True

        # existing implementation below:
        client._host_keys.add(hostname, key.get_name(), key)

        # host keys are actually saved in close() function below
        # in order to control ordering.


# keep connection objects on a per host basis to avoid repeated attempts to reconnect

SSH_CONNECTION_CACHE = {}
SFTP_CONNECTION_CACHE = {}

class Connection(ConnectionBase):
    ''' SSH based connections with Paramiko '''

    @property
    def transport(self):
        ''' used to identify this connection object from other classes '''
        return 'paramiko'

    def _cache_key(self):
        return "%s__%s__" % (self._play_context.remote_addr, self._play_context.remote_user)

    def _connect(self):
        cache_key = self._cache_key()
        if cache_key in SSH_CONNECTION_CACHE:
            self.ssh = SSH_CONNECTION_CACHE[cache_key]
        else:
            self.ssh = SSH_CONNECTION_CACHE[cache_key] = self._connect_uncached()
        return self

    def _connect_uncached(self):
        ''' activates the connection object '''

        if not HAVE_PARAMIKO:
            raise AnsibleError("paramiko is not installed")

        port = self._play_context.port or 22
        self._display.vvv("ESTABLISH CONNECTION FOR USER: %s on PORT %s TO %s" % (self._play_context.remote_user, port, self._play_context.remote_addr), host=self._play_context.remote_addr)

        ssh = paramiko.SSHClient()

        self.keyfile = os.path.expanduser("~/.ssh/known_hosts")

        if C.HOST_KEY_CHECKING:
            ssh.load_system_host_keys()

        ssh.set_missing_host_key_policy(MyAddPolicy(self._new_stdin))

        allow_agent = True

        if self._play_context.password is not None:
            allow_agent = False

        try:
            key_filename = None
            if self._play_context.private_key_file:
                key_filename = os.path.expanduser(self._play_context.private_key_file)

            ssh.connect(
                self._play_context.remote_addr,
                username=self._play_context.remote_user,
                allow_agent=allow_agent,
                look_for_keys=True,
                key_filename=key_filename,
                password=self._play_context.password,
                timeout=self._play_context.timeout,
                port=port,
            )
        except Exception as e:
            msg = str(e)
            if "PID check failed" in msg:
                raise AnsibleError("paramiko version issue, please upgrade paramiko on the machine running ansible")
            elif "Private key file is encrypted" in msg:
                msg = 'ssh %s@%s:%s : %s\nTo connect as a different user, use -u <username>.' % (
                    self._play_context.remote_user, self._play_context.remote_addr, port, msg)
                raise AnsibleConnectionFailure(msg)
            else:
                raise AnsibleConnectionFailure(msg)

        return ssh

    def exec_command(self, cmd, tmp_path, in_data=None, sudoable=True):
        ''' run a command on the remote host '''

        super(Connection, self).exec_command(cmd, tmp_path, in_data=in_data, sudoable=sudoable)

        if in_data:
            raise AnsibleError("Internal Error: this module does not support optimized module pipelining")

        bufsize = 4096

        try:
            self.ssh.get_transport().set_keepalive(5)
            chan = self.ssh.get_transport().open_session()
        except Exception as e:
            msg = "Failed to open session"
            if len(str(e)) > 0:
                msg += ": %s" % str(e)
            raise AnsibleConnectionFailure(msg)

        # sudo usually requires a PTY (cf. requiretty option), therefore
        # we give it one by default (pty=True in ansble.cfg), and we try
        # to initialise from the calling environment
        if C.PARAMIKO_PTY:
            chan.get_pty(term=os.getenv('TERM', 'vt100'), width=int(os.getenv('COLUMNS', 0)), height=int(os.getenv('LINES', 0)))

        self._display.vvv("EXEC %s" % cmd, host=self._play_context.remote_addr)

        no_prompt_out = ''
        no_prompt_err = ''
        become_output = ''

        try:
            chan.exec_command(cmd)
            if self._play_context.prompt:
                if self._play_context.become and self._play_context.become_pass:
                    while True:
                        debug('Waiting for Privilege Escalation input')
                        if self.check_become_success(become_output) or self.check_password_prompt(become_output):
                            break
                        chunk = chan.recv(bufsize)
                        print("chunk is: %s" % chunk)
                        if not chunk:
                            if 'unknown user' in become_output:
                                raise AnsibleError(
                                    'user %s does not exist' % become_user)
                            else:
                                raise AnsibleError('ssh connection ' +
                                    'closed waiting for password prompt')
                        become_output += chunk
                    if not self.check_become_success(become_output):
                        if self._play_context.become:
                            chan.sendall(self._play_context.become_pass + '\n')
                    else:
                        no_prompt_out += become_output
                        no_prompt_err += become_output
        except socket.timeout:
            raise AnsibleError('ssh timed out waiting for privilege escalation.\n' + become_output)

        stdout = ''.join(chan.makefile('rb', bufsize))
        stderr = ''.join(chan.makefile_stderr('rb', bufsize))

        return (chan.recv_exit_status(), '', no_prompt_out + stdout, no_prompt_out + stderr)

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to remote '''

        super(Connection, self).put_file(in_path, out_path)

        self._display.vvv("PUT %s TO %s" % (in_path, out_path), host=self._play_context.remote_addr)

        if not os.path.exists(in_path):
            raise AnsibleFileNotFound("file or module does not exist: %s" % in_path)

        try:
            self.sftp = self.ssh.open_sftp()
        except Exception as e:
            raise AnsibleError("failed to open a SFTP connection (%s)" % e)

        try:
            self.sftp.put(in_path, out_path)
        except IOError:
            raise AnsibleError("failed to transfer file to %s" % out_path)

    def _connect_sftp(self):

        cache_key = "%s__%s__" % (self._play_context.remote_addr, self._play_context.remote_user)
        if cache_key in SFTP_CONNECTION_CACHE:
            return SFTP_CONNECTION_CACHE[cache_key]
        else:
            result = SFTP_CONNECTION_CACHE[cache_key] = self._connect().ssh.open_sftp()
            return result

    def fetch_file(self, in_path, out_path):
        ''' save a remote file to the specified path '''

        super(Connection, self).fetch_file(in_path, out_path)

        self._display.vvv("FETCH %s TO %s" % (in_path, out_path), host=self._play_context.remote_addr)

        try:
            self.sftp = self._connect_sftp()
        except Exception as e:
            raise AnsibleError("failed to open a SFTP connection (%s)", e)

        try:
            self.sftp.get(in_path, out_path)
        except IOError:
            raise AnsibleError("failed to transfer file from %s" % in_path)

    def _any_keys_added(self):

        added_any = False
        for hostname, keys in self.ssh._host_keys.iteritems():
            for keytype, key in keys.iteritems():
                added_this_time = getattr(key, '_added_by_ansible_this_time', False)
                if added_this_time:
                    return True
        return False

    def _save_ssh_host_keys(self, filename):
        '''
        not using the paramiko save_ssh_host_keys function as we want to add new SSH keys at the bottom so folks
        don't complain about it :)
        '''

        if not self._any_keys_added():
            return False

        path = os.path.expanduser("~/.ssh")
        makedirs_safe(path)

        f = open(filename, 'w')

        for hostname, keys in self.ssh._host_keys.iteritems():

            for keytype, key in keys.iteritems():

                # was f.write
                added_this_time = getattr(key, '_added_by_ansible_this_time', False)
                if not added_this_time:
                    f.write("%s %s %s\n" % (hostname, keytype, key.get_base64()))

        for hostname, keys in self.ssh._host_keys.iteritems():

            for keytype, key in keys.iteritems():
                added_this_time = getattr(key, '_added_by_ansible_this_time', False)
                if added_this_time:
                    f.write("%s %s %s\n" % (hostname, keytype, key.get_base64()))

        f.close()

    def close(self):
        ''' terminate the connection '''

        cache_key = self._cache_key()
        SSH_CONNECTION_CACHE.pop(cache_key, None)
        SFTP_CONNECTION_CACHE.pop(cache_key, None)

        if self.sftp is not None:
            self.sftp.close()

        if C.HOST_KEY_CHECKING and C.PARAMIKO_RECORD_HOST_KEYS and self._any_keys_added():

            # add any new SSH host keys -- warning -- this could be slow
            lockfile = self.keyfile.replace("known_hosts",".known_hosts.lock")
            dirname = os.path.dirname(self.keyfile)
            makedirs_safe(dirname)

            KEY_LOCK = open(lockfile, 'w')
            fcntl.lockf(KEY_LOCK, fcntl.LOCK_EX)

            try:
                # just in case any were added recently

                self.ssh.load_system_host_keys()
                self.ssh._host_keys.update(self.ssh._system_host_keys)

                # gather information about the current key file, so
                # we can ensure the new file has the correct mode/owner

                key_dir  = os.path.dirname(self.keyfile)
                key_stat = os.stat(self.keyfile)

                # Save the new keys to a temporary file and move it into place
                # rather than rewriting the file. We set delete=False because
                # the file will be moved into place rather than cleaned up.

                tmp_keyfile = tempfile.NamedTemporaryFile(dir=key_dir, delete=False)
                os.chmod(tmp_keyfile.name, key_stat.st_mode & 0o7777)
                os.chown(tmp_keyfile.name, key_stat.st_uid, key_stat.st_gid)

                self._save_ssh_host_keys(tmp_keyfile.name)
                tmp_keyfile.close()

                os.rename(tmp_keyfile.name, self.keyfile)

            except:

                # unable to save keys, including scenario when key was invalid
                # and caught earlier
                traceback.print_exc()
                pass
            fcntl.lockf(KEY_LOCK, fcntl.LOCK_UN)

        self.ssh.close()

