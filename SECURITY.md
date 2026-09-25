# Security policy

Report vulnerabilities privately through GitHub's **Report a vulnerability** feature for this repository. Do not post credentials, ARC configuration dumps, serial numbers, or unredacted diagnostics in public issues.

The integration connects only to the configured local ARC using CGI HTTP digest authentication and DHIP TCP. These protocols are device-provided and are not encrypted. Use a trusted, segmented local network; do not expose the ARC ports to the Internet.

Version 0.1.x is the supported public release line. Security fixes will be published as patch releases when feasible.
