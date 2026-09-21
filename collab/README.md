Storage Metrics
===============

Script for getting the Collaboration Storage Utilization numbers for the
monthly reports.

This queries the Kubernetes clusters we can kubectl to (nautilus, tempest,
tiger), detecting pelican origin Deployments (based on the pod template's
image name), exec'ing into a pod resolved from each Deployment to collect
the amount of storage used by each export in the federation, then grouping
them by collaboration into a table that shows:

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

3. Run `./storage_metrics.py`

4. Fill in the table in the monthly report Google Doc with the numbers
   given in the summary table printed at the end of the program.

See `./storage_metrics.py --help` for additional arguments.


Configuration
-------------



`config.ini` defines:

* Which Kubernetes namespaces to look at.
* Which origins (based on Deployment names) to ignore.
* Which Pelican namespaces (federation prefixes) to ignore.
* The mapping between federation prefixes and collaborations.

Mechanism and Limitations
-------------------------
