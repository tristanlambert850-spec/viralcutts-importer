# Viralcutts YouTube importer

Prepared backend; not deployed or verified against YouTube yet.

Deploy this directory as a Docker web service on Render with the Free instance type, or another Python/Docker host. Keep one process/instance because jobs are stored in memory. Set DEMO_ACCESS_CODE to a private demo code and ALLOWED_ORIGIN to https://tax1234-viralcutts.static.hf.space. No credentials belong in the public website source. Give the code only to demo users, who enter it into the importer.

After deployment, put the HTTPS service origin in the frontend's YOUTUBE_API constant. Then publish the companion index.html to the existing Hugging Face Space. Do not publish the draft frontend claiming link import is connected before this is done.

The importer accepts individual HTTPS YouTube watch, Shorts and youtu.be links; playlist and arbitrary download URLs are rejected. It retrieves compatible MP4 up to 480p, 15 minutes, and 150 MB. One job runs at a time, at most three jobs are retained, and expired jobs are removed on subsequent requests after ten minutes. Hosting restarts discard all imports. Clip editing/export continues in the browser.

Use videos you own or have permission to edit. YouTube may block cloud server IPs or require authentication. This service does not bypass sign-in, private videos or access restrictions; it reports the failure and leaves local upload available. A free host does not guarantee YouTube imports will succeed. yt-dlp is intentionally installed at its current release at build time because YouTube frequently changes; rebuild when extractor fixes are released.

Render free services sleep when idle and have traffic limits. This is a limited demo service, not a production deployment.
