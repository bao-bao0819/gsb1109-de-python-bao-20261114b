.. TFTPy documentation master file, created by
   sphinx-quickstart on Sun Jul 11 18:48:32 2010.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

TFTPy
=====

TFTPy is a pure python TFTP implementation.

.. toctree::
   :maxdepth: 2

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`

Requirements
============
Python 3.8+, I think. I am not testing in all intermediate Python versions, so let me know if you find
any problems.

Installation
============
If you download the source distribution, you can simply use distutils to
install, via::

    python setup.py build
    python setup.py install

Or, as this has been uploaded to pypi, you can use easy_install or pip::

    easy_install tftpy
    pip install tftpy

Once installed you should have the sample client and server scripts in bin,
and you should be able to import the `tftpy` module.

Examples
========
The simplest tftp client::

    import tftpy

    client = tftpy.TftpClient('tftp.digitaltorque.ca', 69)
    client.download('remote_filename', 'local_filename')

The simplest tftp server::

    import tftpy

    server = tftpy.TftpServer('/tftpboot')
    server.listen('0.0.0.0', 69)

See the sample client and server for slightly more complex examples.

Offline transfer diagnosis
==========================
When a transfer is reported complete on the sender but the receiver only
stored part of the file, capture structured packet traces on both peers and
align them offline. Traces are independent of the text log level and carry
both a wall clock and a monotonic timestamp.

Enable tracing (no tracing happens unless a target is supplied)::

    bin/tftpy_client.py --trace client.jsonl --upload name -i name ...
    bin/tftpy_server.py --root /tftpboot --trace-dir /var/log/tftp/ ...

Then build the receiver storage digest and run the alignment tool. The clock
skew bound must cover the worst-case difference between the two host clocks
(including NTP and timezone uncertainty)::

    python -m tftpy.offline_diag \
        --sender-trace client.jsonl \
        --receiver-trace /var/log/tftp/tftp-server-*.jsonl \
        --storage-file /tftpboot/name \
        --expected-size 1048576 \
        --clock-skew 2 \
        --format text

The tool never opens a socket and never modifies the transferred file. It
reports three block ranges:

* **SENT_NOT_SEEN** - DAT blocks the sender transmitted with no receive event
  at the receiver.
* **SEEN_NOT_ACKED** - DAT blocks the receiver observed but for which the
  sender has no ACK receive event.
* **ACKED_NOT_STORED** - confirmed data blocks that the receiver storage
  digest cannot account for (empty terminal DAT and control ACK 0 excluded).

Cross-side ordering is only asserted when a timestamp difference exceeds the
skew band; differences inside the band are reported as
``indeterminate-in-skew-band`` and are never used, on their own, to blame one
peer. Where the evidence cannot narrow the fault to a single stage, every
stage still consistent with the observations is retained, each with the exact
file, function and fields to instrument next. Existing tftpy text logs can be
passed in place of JSONL traces and are parsed on a best-effort basis.

API Documentation
=================

Front-end Modules
-----------------
These modules are the ones that you will need to use directly to implement a
TFTP client or server.

The :mod:`tftpy` Module
~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy
   :members:
   :show-inheritance:

The `TftpClient` Module
~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.TftpClient
   :members:
   :show-inheritance:

The `TftpServer` Module
~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.TftpServer
   :members:
   :show-inheritance:

Back-end Modules
----------------

The `TftpPacketFactory` Module
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.TftpPacketFactory
   :members:
   :show-inheritance:

The `TftpPacketTypes` Module
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.TftpPacketTypes
   :members:
   :show-inheritance:

The `TftpShared` Module
~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.TftpShared
   :members:
   :show-inheritance:

The `TftpContexts` Module
~~~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.TftpContexts
   :members:
   :show-inheritance:

The `TftpStates` Module
~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.TftpStates
   :members:
   :show-inheritance:

The `transfer_trace` Module
~~~~~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.transfer_trace
   :members:
   :show-inheritance:

The `offline_diag` Module
~~~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.offline_diag
   :members:
   :show-inheritance:
