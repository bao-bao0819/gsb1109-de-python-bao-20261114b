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

Recovering incomplete receive replicas
=======================================
A receive backup that lost packets or was interrupted can be completed from
other replicas of the same transfer.  Recovery accepts a block combination
only if it satisfies the version declaration and every device-generated
segment digest; digests may cover several TFTP blocks.  If more than one legal
completion exists, only ranges that are identical in all of them are
delivered, and the rest are reported as pending::

    import tftpy

    replica_a = tftpy.Replica('a', {1: b'first bl', 2: b'ock two'})
    replica_b = tftpy.Replica('b', {3: b' last bl', 4: b''})
    declaration = tftpy.VersionDeclaration(size=24, blocksize=8, version='v2')

    result = tftpy.recover_file([replica_a, replica_b], declaration)
    result.write('recovered.bin')
    print(result.blocks[2].sources)  # replicas usable for block 2

Both the final short block and an empty block carry their source replicas,
so a confirmed empty file is still attributable.

A manifest-driven batch run is available from the module::

    python -m tftpy.TftpRecovery manifest.json -o output/

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

The `TftpRecovery` Module
~~~~~~~~~~~~~~~~~~~~~~~~~
.. automodule:: tftpy.TftpRecovery
   :members:
   :show-inheritance:
