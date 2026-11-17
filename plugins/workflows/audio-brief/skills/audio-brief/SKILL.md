---
name: audio-brief
description: Use when someone asks to listen to a completed report, hear the top buying signals before a meeting, or get a narrated research summary with sources. Uses the team's own TTS Buddy tool.
metadata:
  summary: Turns a report into a downloadable MP3 with the original source links using the team's TTS Buddy account.
---

# Listen to this brief

Start with the report the user wants to hear. Deliver an MP3 and a short list of sources in the same order as the recording.

## Prepare the brief

For “Which three buying signals from this week are worth following up on? Make me a two-minute audio brief and include the sources,” use the ranked findings from `lead-signals`. If the report is already in the conversation, reuse it. If the user requested fresh research, finish that workflow first.

For each selected signal, say what happened, why it matters for this buyer and one question to ask next. Keep observations distinct from inferences. If only two signals have evidence, narrate two. For another kind of report, use the main findings and their implications. Keep the original source titles and URLs in a numbered list. Mention missing citations; never invent a link. Leave raw URLs and unnecessary contact details out of the narration.

Write the script in the requested spoken language. For a two-minute English brief, aim for about 250 words; the actual duration depends on the voice and speed. For another language, choose a supported voice/language pair from the [TTS Buddy API reference](https://www.ttsbuddy.com/docs/developers/api-reference#available-voices). Supertonic voices accept a separate language code, e.g. `{"voice":"st_f1","language":"es"}` for a Spanish script. Standard voices determine their language from the voice ID.

## Connect the team's tool

Look for `ttsbuddy` with `treg tool ls` or `my_tools` on treg's team MCP connector (`/mcp/`). Check that the base URL is `https://www.ttsbuddy.com`. If it points elsewhere, stop before sending the report and ask the admin to check the binding. If the tool is missing, give a team admin the following one-time setup. The admin supplies their own protected key file and replaces SECRET_ID with the ID from the first command. Keep the key out of chat and the report.

```sh
treg secret add ttsbuddy-key --file /path/to/protected/key
treg tool add ttsbuddy --base-url https://www.ttsbuddy.com --secret SECRET_ID
```

treg adds the bearer credential on the server. Narration uses the team's [TTS Buddy plan](https://www.ttsbuddy.com/docs/pricing/plans); the team's own tool consumes no treg balance.

## Generate or resume

1. In a private working directory for this brief, save the sources, the exact JSON payload as `request.json`, and a fresh UUID as `request-key.txt` **before** the first call. Include `text` with the actual script and any selected voice, language and speed. For a changed script or settings, start a new request. When resuming, reuse the saved files.
2. If a job ID is already saved, go straight to the status check. Otherwise submit through treg:

   ```sh
   treg --json call ttsbuddy v1/agent-tts --method POST \
     --header "Idempotency-Key: $(cat request-key.txt)" --file request.json
   ```

   On MCP, use `call` with `endpoint_id="ttsbuddy/v1/agent-tts"`, `method="POST"`, `body` equal to the saved JSON object and `idempotency_key` equal to the saved UUID. Read the provider response under CLI `result` or MCP `body`. Save its `job_id` immediately as `job-id.txt`. If the submission response was lost, retry through the original interface with the saved key and unchanged request bytes. On a 422 idempotency mismatch, restore the original payload. On a 409 indicating the call is in progress, wait before retrying. If treg reports that the outcome is unknown or the response is lost (410), use a previously saved provider job ID if available. Otherwise return the treg call ID and explain the uncertainty; a new submission could generate the brief twice.
3. Inspect both the HTTP status and the provider's status. A successful `completed` response may arrive inline. For `processing`, wait the indicated `retry_after_seconds` or Retry-After when available; otherwise wait five seconds. Poll through the same team tool:

   ```sh
   treg --json call ttsbuddy v1/agent-tts --query "id=$(cat job-id.txt)"
   ```

   On MCP, call the same endpoint with `method="GET"` and `params={"id":"<saved job ID>"}`. Back off on rate limits. If the run ends before the job does, keep its ID for the next run. Report a terminal `failed` or `expired` response, even on HTTP 200. Surface credential and quota errors to the user.
4. Once `success` is true and `status` is `completed`, download the temporary HTTPS `audio_url` promptly using a fresh request without treg or provider credentials. If the download is interrupted or the URL expires, look up the saved job for a fresh URL. Download to a temporary file, verify the download succeeded and a media reader recognizes an MP3 audio stream (for example, with `ffprobe`), then rename it to `brief.mp3`. Attach the saved file or provide its accessible local path, followed by the numbered sources. If this client cannot save or attach files, label the URL as temporary and give the user the job ID for recovery.
