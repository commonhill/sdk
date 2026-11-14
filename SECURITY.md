# Security

## Reporting a vulnerability

Email **security@commonhill.org** with what you found, how to reproduce it, and what it lets
someone do, or send it from the site's contact page (<https://commonhill.org/contact>, about "A
security problem"). Please don't open a public issue or pull request for it.

We acknowledge reports within three working days. That is a reply, not a fix: we'll tell you what
we think of the report and keep you posted until it's fixed or we decide it isn't a vulnerability.
Commonhill is run by one person, so a fix may take longer than at a larger project.

## What counts

Anything in the grader SDK or the kits that lets a candidate change its own result, for example:

- writing or forging the grader's result, or making the grader accept a malformed one;
- getting out of a kit's sandbox, or past its instruction or cycle cap;
- learning anything the grader keeps from the candidate, such as held-out items, values or seeds.

Problems in the platform itself (the VMs, the API, the ledger) belong to Commonhill's core: the
same address takes them.

## Supported versions

Fixes go to `main` and ship in the next release. A project picks a fix up when it vendors that
release.
