FROM curlimages/curl:8.11.1
# Runs once per cron firing: POST /jobs/tick with the shared secret, then exits.
CMD ["sh", "-c", "curl -fsS -X POST \"$API_BASE/jobs/tick\" -H \"X-Cron-Secret: $CRON_SECRET\" --max-time 240"]
