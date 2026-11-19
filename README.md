# Local Quiz Assistant

This local web page starts the browser automation on this computer so it can use the existing `browser_profile`. The page binds only to `127.0.0.1`; it does not upload the browser profile or API key.

## Setup

1. Install the Python packages:

   ```powershell
   python -m pip install -r requirements.txt
   python -m playwright install chromium
   ```

2. Start the local page from the project folder:

   ```powershell
   python web_app.py
   ```

   On Windows, you can also double-click `run.bat`.

The page opens at `http://127.0.0.1:8765`. The first time, paste your Groq API key into the page and choose **Save key**. It is stored in the local `.env` file and stays there until you replace it. The key is not displayed again or included in activity logs. Keep `.env` private; it is ignored by Git.

Enter the assignment URL and press **Start**. The browser opens separately; sign in there if needed. By default, start the assignment manually in the browser. The optional automatic start may consume an attempt. The runner stops before final submission.

Use **Stop** in the page to request a safe stop. Model/API calls may take a few seconds to finish. The automation browser remains open for review; press **Close browser** in the page when you are done.

The existing command-line entry point remains available with `python agent.py`. It waits at the end so you can review the assignment in Chrome and submit it yourself if ready; press ENTER in the terminal only when you want to close Chrome.

The command-line runner and dashboard use the same `browser_profile`, so do not run them at the same time. To pick up code changes, stop an already-running dashboard with Ctrl+C and restart it with `python web_app.py`.
