# Repository copies are not the operative policies

The Terms of Service and Privacy Policy source files in this repository
(`apps/wyrdfold/src/app/(public)/terms/` and `.../privacy/`) are published for
transparency — so anyone can read what the hosted service claims and check it
against the code that implements it.

They are not the operative agreement. The binding terms governing your use of
the hosted WyrdFold service are the versions rendered at
<https://wyrdfold.com/terms> and <https://wyrdfold.com/privacy>.

Those pages resolve the operating entity's legal name and registered address
from environment variables at build time. Those values are deliberately not
included in this repository. A copy of these files taken from git is therefore
incomplete by design, and an edited copy in a fork is not an amendment to
anything.

If the rendered pages and this repository ever disagree, the rendered pages
govern.

## If you self-host or fork

The [FSL-1.1-ALv2](./LICENSE.md) permits you to self-host and modify WyrdFold,
subject to its terms. It does not make WyrdFold's hosted-service Terms of
Service or Privacy Policy applicable to your deployment.

If you self-host, the policy text here is a starting point that you are
responsible for adapting. It describes our sub-processors, our retention
practices, and our jurisdiction, none of which are automatically true of your
deployment.

Two consequences are worth stating plainly:

- **You are responsible for your own compliance.** If you operate a self-hosted
  instance, you are responsible for determining your role under applicable
  data-protection law and meeting the obligations that apply to your
  deployment. We are not in a position to make that determination for you.
- **Your policy text governs your deployment, not ours.** Publishing a modified
  Privacy Policy from a fork creates no obligation on the operator of
  wyrdfold.com, and we are not a party to any agreement you form with your
  users.

We do not vet, approve, endorse, or assume responsibility for third-party
self-hosted instances, regardless of whether they use our source code or
modified versions of our policies.

## Not legal advice

Nothing in this repository is legal advice. If you operate a service that
processes personal data, get advice from a qualified lawyer in your
jurisdiction.
