Storage Metrics
===============

Script for getting the Collaboration Storage Utilization numbers for the
monthly reports.

This queries the Kubernetes clusters we can kubectl to (Nautilus, Tempest, Tiger),
entering Pelican Origin pods to collect the amount of storage used by each export in the federation,
then grouping them by collaboration into a table that shows:

* the collaboration name
* the amount of authenticated data
* the amount of public data

Data in a Pelican origin for a namespace is considered public if
"PublicReads" is in the capabilities for that namespace, and authenticated
if "PublicReads" is not in the capabilities.


Requirements
------------

* Python 3.9
* kubectl
* (not yet) AWS CLI


Usage
-----

1. Set up your kubeconfig file to have separate contexts for Nautilus,
   Tempest, and Tiger.  (The contexts should be named "nautlius",
   "tempest", and "tiger", though that can be changed with a command-line
   argument.)

2. Obtain credentials for Nautilus, Tempest, and Tiger.  Make sure you
   can `get deployments`, `get pods`, and `exec` in the namespaces that have
   origins in them.

3. Run `./storage_metrics.py`.

4. Fill in the table in the monthly report Google Doc with the numbers
   given in the summary table printed at the end of the program.

See `./storage_metrics.py --help` for additional arguments.


Mechanism
---------

storage_metrics gets each Deployment in the configured Kubernetes cluster and namespace,
detects if they're origins by looking for an image named "osdf-origin" or "origin",
then exec'ing into them to get the disk space used by various exports.

There is an outer script (storage_metrics) and an inner script (inner.py);
the inner script gets copied into the origin and run from inside;
the outer script reads and interprets the data returned by the inner script.

For POSIX and POSIXv2 origins, the script uses the `ceph.dir.rbytes` extended attribute (if available),
or `du -bs` (if not).  These actions are run inside the container by an inner script.

For S3 origins, since the tools to query disk used in a bucket are not available in the container,
the access key and secret key are retrieved from the container,
and used in a callout to the `aws` CLI tool, outside the container.
You must have the AWS tool installed to get the amount of data inside S3 origins.


Configuration
-------------

`config.ini` defines:

* Which Kubernetes namespaces to look at.
* Which origins (based on Deployment names) to ignore.
  The script looks at all origins except those that are specifically excluded.
* Which Pelican namespaces (federation prefixes) to ignore.
  The script looks at all namespaces except those that are specifically excluded.
* The mapping between federation prefixes and collaborations.
* The "sub-namespace" mapping for origins that hold multiple collaborations within a single namespace.


Limitations
-----------

* Globus, XRootD, and HTTP, and other origin types are not supported.
* None of the collabs we keep track of have S3 origins, so the S3 functionality hasn't been exercised.
* The origin must be up and running for the data to be collected.
