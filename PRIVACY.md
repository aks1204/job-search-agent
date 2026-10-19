# Privacy policy — Job Agent

Job Agent is a self-hosted, single-user tool. Each copy runs on its owner's own computer or server and
is used only by that owner. There is no central service: the authors of this project receive no data
from anyone's copy.

## What the app accesses in your Google account

When you connect Gmail, you grant two permissions to **your own copy** of the app:

- **Send email on your behalf** (`gmail.send`): used only to send outreach emails that you have
  reviewed and approved one by one inside the app. Nothing is sent without your approval.
- **Read your email** (`gmail.readonly`): used for two narrow purposes only:
  1. the email threads the app itself started, to notice replies and bounces;
  2. emails **from hiring systems** (Workday, Greenhouse, Lever and similar) or from companies whose jobs
     you are tracking, from the last 60 days, to notice "application received" and later status emails
     (in review, interview, offer, rejection) for jobs you applied to.
  The app does not open any other email. From the emails it does read, it keeps only facts (which job,
  what happened, when, and a link to the email in your Gmail), not their content.

## Where your data is kept

Everything stays on the machine running your copy of the app: its database holds the emails it sent,
the replies and bounces it noticed in those threads, and your encrypted Gmail sign-in. Nothing is
shared with the project authors or sold to anyone. The app sends text to the AI provider you configure
(Anthropic) to draft and check emails and to sort the replies and hiring-system emails described above, and uses the optional lookup services you configure with your
own keys.

## Your control

- Disconnect at any time at https://myaccount.google.com/permissions.
- Delete the app's data by removing its database on your machine.

## Contact

Questions: open an issue at https://github.com/aks1204/job-search-agent/issues
