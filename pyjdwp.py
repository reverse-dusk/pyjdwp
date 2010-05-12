#!/usr/bin/env python3.1
"""pyjdwp a Java Debugger in Python

The default debugger (jdb) is so clunky and lacks a vi mode (it actually
lacks a proper readline support). This implementation makes use of the
excellent cmd module to create a proper shell-style debugger... like pdb.

"""
import cmd
import atexit
import os
import readline
import getpass
import sys
import time
import tempfile
import re
import socket
import struct
from optparse import OptionParser

import parser

cmd_map = {
    "Version":              (1, 1),
    "ClassesBySignature":   (1, 2),
    "AllClasses":           (1, 3),
    "AllThreads":           (1, 4),
    "TopLevelThreadGroups": (1, 5),
    "Dispose":              (1, 6),
    "IDSizes":              (1, 7),
    "Suspend":              (1, 8),
    "Resume":               (1, 9),
    "Exit":                 (1, 10),
    "CreateString":         (1, 11),
    "Capabilities":         (1, 12),
    "ClassPaths":           (1, 13),
    "DisposeObjects":       (1, 14),
    "HoldEvents":           (1, 15),
    "ReleaseEvents":        (1, 16),
}

jni_types = ("[", "L", "Z", "B", "C", "S", "I", "J", "F", "D")


class PyJDWP(cmd.Cmd):
    default_field = 'contents'
    select = 'name path'
    limit = 5
    sort_on = ''
    analyzer = 'Simple'
    waterline = ''
    trunc_size = 150

    def __init__(self):
        cmd.Cmd.__init__(self)
        self.prompt = 'pyjdwp> '
        self.is_query = True
        self.config_path = os.path.expanduser("~/.pyjdwp")
        self.history_path = os.path.join(self.config_path, "history")
        self.previous_path = None
        self.query_hist = []

        self.last_error = None

        # Network properties
        self.hostname = "localhost"
        self.port = 8000

        # List of classes at hand for auto complete
        self.classes = [ ]

        self.command_count = 0

        if not os.path.exists(self.config_path):
            os.mkdir(self.config_path)

        if os.path.exists(self.history_path):
            readline.read_history_file(self.history_path)

        print("Connecting...", end="\r")
        sys.stdout.flush()
        if self.connect():
            print("Loading status...", end="\r")
            parser.set_sizes(self.get_sizes())
            print("Connected to {}".format(self.server_string()))
        else:
            print("Failed to connect to {}".format(self.server_string()))
            if self.last_error is not None:
                print(self.last_error)
            self.exit()
            sys.exit(-1)

    def server_string(self):
        """Return a simple host:port server string."""
        return self.hostname + ":" + str(self.port)

    def send_command(self, cmd, data=None):
        """Send a command via the currently opened socket.

        A command, according to the JDWP spec is:
            - length (4 bytes)
            - id (4 bytes)
            - flags (1 byte)
            - command set (1 byte)
            - command (1 byte)

        """
        hformat = ">iiBBB"
        hlength = length = 11
        packet_id = self.command_count
        cmd_set, cmd_id = cmd_map[cmd]

        if data:
            length += len(data)

        # Pack and send the request
        packet = struct.pack(hformat, length, packet_id, 0, cmd_set, cmd_id)
        if data:
            packet = packet + data
        self.socket.send(packet)

        # Extract the header from the response
        response_header = struct.unpack(hformat, self.socket.recv(hlength))
        (length, packet_id, flags, cmd_set, cmd) = response_header

        self.command_count += 1

        full_length = length - hlength
        data = b""
        while len(data) < full_length:
            data += self.socket.recv(length - hlength)
        return data

    def connect(self):
        """Connect and send the handshake with the server."""
        magic_token = b"JDWP-Handshake"
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.connect((b"localhost", 8000))
        except Exception as e:
            self.socket = None
            self.last_error = str(e)
            return False
        self.socket.send(magic_token)
        data = self.socket.recv(14)
        if data == magic_token:
            return True
        else:
            return False

    def exit(self):
        """Exit pyjdwp, closing the connection."""
        print("")
        if self.socket:
            self.socket.close()
        readline.write_history_file(self.history_path)

    #
    # Relatively direct JDWP methods
    #
    def capabilities(self):
        """Retrieve this VM's capabilities. The capabilities are returned as
        booleans, each indicating the presence or absence of a capability. The
        commands associated with each capability will return the
        NOT_IMPLEMENTED error if the cabability is not available.
        """
        response = self.send_command("Capabilities")
        unpacked = parser.unpack(response, "???????")
        return tuple(unpacked)

    def classpaths(self):
        """Retrieve the classpath and bootclasspath of the target VM. If the
        classpath is not defined, returns an empty list. If the bootclasspath
        is not defined returns an empty list.
        """
        response = self.send_command("ClassPaths")
        (base_dir, count, off) = parser.unpack(response, "si", add_offset=True)

        classpaths = [ ]

        while len(classpaths) < count:
            path = parser.unpack(response[off:], "s", add_offset=True)
            off += path.pop()
            path = path.pop()
            classpaths.append(path)

        return (classpaths, [ ])
        
        # unpacked = parser.unpack(response, "???????")
        # return tuple(unpacked)

    def create_string(self, s):
        """Creates a new string object in the target VM and returns its id."""
        packed_s = parser.pack_string(s)
        response = self.send_command("CreateString", data=packed_s)
        unpacked = parser.unpack(response, "o")
        return unpacked.pop()
        
    def dispose_vm(self):
        """Invalidates this virtual machine mirror. The communication channel
        to the target VM is closed, and the target VM prepares to accept
        another subsequent connection from this debugger or another
        debugger.
        """
        self.send_command("Dispose")

    def exit_vm(self, exit_code=0):
        """Terminates the target VM with the given exit code. All ids
        previously returned from the target VM become invalid. Threads running
        in the VM are abruptly terminated. A thread death exception is not
        thrown and finally blocks are not run.
        """
        packed_code = parser.pack_int(exit_code)
        self.send_command("Exit", data=packed_code)

    def get_classes(self):
        """Returns the list of all the classes currently loaded."""
        format = "brsi"
        data = self.send_command("AllClasses")
        count = parser.unpack_int(data)
        classes = [ ]
        offset = 4

        while len(classes) < count:
            class_data = parser.unpack(data[offset:], format, add_offset=True)
            offset += class_data.pop()
            (cid, cadd, sig, status) = class_data
            while sig and sig[0] in jni_types:
                sig = sig[1:]
            while sig and sig[-1] in (";",):
               sig = sig[:-1]
            sig = sig.split("$")[0]
            sig = sig.replace("/", ".")
            classes.append(sig)

        return sorted(list(set(classes)))

    def get_sizes(self):
        """Load the object sizes in the instance."""
        return parser.unpack(self.send_command("IDSizes"), "iiiii")

    def get_threads(self):
        """Return a list of threads."""
        data = self.send_command("AllThreads")
        count = parser.unpack_int(data)
        offset = 4
        threads = [ ]

        while len(threads) < count:
            thread_data = parser.unpack(data[offset:], "o", add_offset=True)
            offset += thread_data.pop()
            thread_id = thread_data.pop()
            threads.append(thread_id)

        return threads

    def get_threadgroups(self):
        """Return a list of thread groups."""
        data = self.send_command("TopLevelThreadGroups")
        count = parser.unpack_int(data)
        offset = 4
        groups = [ ]

        while len(groups) < count:
            group_data = parser.unpack(data[offset:], "o", add_offset=True)
            offset += group_data.pop()
            group_id = group_data.pop()
            groups.append(group_id)

        return groups

    def resume_vm(self):
        self.send_command("Resume")

    def suspend_vm(self):
        self.send_command("Suspend")

    #
    # Cmd aliases
    #
    def do_capabilities(self, msg):
        """Return the VM's capabilities."""
        caps = self.capabilities()
        print("""
        canWatchFieldModification: {}
        canWatchFieldAccess: {}
        canGetBytecodes: {}
        canGetSyntheticAttribute: {}
        canGetOwnedMonitorInfo: {}
        canGetCurrentContendedMonitor: {}
        canGetMonitorInfo: {}
        """.format(*caps))

    def do_classes_by_signature(self, msg):
        """Returns the list of all the classes by signature (TODO)."""
        signature = parser.pack_string("Ljava/lang/String;")
        data = self.send_command("ClassesBySignature", data=signature)
        print(str(data))

    def do_classes(self, msg):
        """Print the list of all the classes currently loadeds."""
        if not self.classes:
            self.classes = self.get_classes()
        for c in self.classes:
            print(c)

    def do_classpaths(self, msg):
        """Retrieve the classpath and bootclasspath of the target VM. If the
        classpath is not defined, returns an empty list. If the bootclasspath
        is not defined returns an empty list.
        """
        print(str(self.classpaths()))

    def do_create_string(self, msg):
        """Creates a new string object in the target VM and returns its id."""
        print(self.create_string(msg))

    def do_exit(self, msg):
        self.exit()
        return -1
    do_quit = do_EOF = do_exit

    def do_exit_vm(self, msg):
        self.exit_vm()
        return self.do_exit(self, msg)

    def do_resume(self, msg):
        """Resume the VM."""
        self.send_command("Resume")

    def do_sizes(self, msg):
        """Print five ints defining the size of certain objects."""
        print(str(self.get_sizes()))

    def do_suspend(self, msg):
        """Suspend the VM."""
        self.send_command("Suspend")

    def do_threads(self, msg):
        """Print all the current thread ids.""" 
        print(str(self.get_threads()))

    def do_threadgroups(self, msg):
        """Print all the top level thread groups."""
        print(str(self.get_threadgroups()))

    def do_vmversion(self, msg):
        """Returns the VM version number."""
        f = "siiss"
        data = parser.unpack(self.send_command("Version"), f)
        (description, jdwp_major, jdwp_minor, vm_version, vm_name) = data
        print(description)

if __name__ == "__main__":
    app = PyJDWP()
    try:
        app.cmdloop()
    except KeyboardInterrupt:
        print("Interrupting...")
        app.do_exit()

