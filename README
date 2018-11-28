# A dCache plugin for handling small files on tape backends more efficiently

This project is an example for writing custom HSM scripts for the dCache storage system. It is not an official part of dCache and does not receive support from the dCache.org team.

The use case for this plugin is storing large amounts of comparatively small files (below the GB range) on tape. Due to the inherent properties of tape systems, storing small files is not space-efficient. This plugin transparently aggregates files from dedicated dCache pools into large archives for tape storage. The archives are not visible to users.

## Repository structure

The `skel` directory contains the actual source code, and using the `.spec` files in the root directory, installation RPMs can be generated for the packer host as well as for the pool nodes. An installer script is also provided.

Documentation is provided in the form of IPython notebooks in the directory `main/ipy`. Some of those IPython files can also serve as interactive installation notebooks for machines where IPython is available. Others provide analysis frameworks for evaluating system performance.
