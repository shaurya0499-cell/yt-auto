# Free YouTube Automation (daily Short + weekly long-form)

Runs entirely on free tiers: GitHub Actions (scheduler), Gemini (scripts/titles/descriptions),
Edge-TTS (voice), Pexels (stock footage), FFmpeg (editing), YouTube Data API (upload).

- **Every day:** picks a fresh topic from your RSS feeds, writes a ~45s commentary script,
  voices it, adds captioned stock footage, uploads as a Short with title + description + tags.
- **Every Sunday:** turns the week's Shorts into a ~6-7 minute recap with chapters, uploads it.

## One-time setup (about 20 minutes)

1. **GitHub**: create a new repo, upload all these files (keep the `.github` folder).
2. **Gemini key** (free): https://aistudio.google.com/apikey  -> secret `GEMINI_API_KEY`
3. **Pexels key** (free): https://www.pexels.com/api/  -> secret `PEXELS_API_KEY`
4. **YouTube API**:
   - console.cloud.google.com -> new project -> enable **YouTube Data API v3**
   - OAuth consent screen: External, add your own Google account as a test user,
     then click **Publish app** (set to "In production"). If you leave it in "Testing",
     your login token expires every 7 days and uploads will break.
   - Credentials -> Create OAuth client ID -> **Desktop app** -> download JSON,
     save it here as `client_secret.json`
   - On your own computer: `pip install google-auth-oauthlib` then `python get_token.py`,
     sign in with the channel's account (ignore the "unverified app" warning).
   - Add the 3 printed values as secrets: `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`
5. **Edit `config.yaml`**: niche, tone, voice, RSS feeds for your topic.
6. Repo -> **Actions** tab -> run `daily-short` manually once (workflow_dispatch) to test.

Test locally without uploading: `python yt_auto.py short --dry-run` (needs the two API keys
as environment variables and ffmpeg installed). Output lands in `out/`.

## Important notes

- Videos upload as **private** by default. Check the first few, then set `privacy: public`
  in `config.yaml`.
- New Google Cloud projects that haven't passed YouTube's API compliance audit get their
  uploads locked to private. If your videos stay private even with `public` set, submit the
  free audit form (search "YouTube API Services audit and quota extension form").
- Quota: the default 10,000 units/day allows about 6 uploads/day, plenty for this.
- Schedule times are in UTC (edit `cron` in `.github/workflows/*.yml`).
- YouTube demonetizes repetitive, mass-produced content. Edit the prompts in `yt_auto.py`
  to push for a distinct opinion/angle, and skim videos while on private.
- Edge-TTS is an unofficial free endpoint; if it ever breaks, swap `synth()` for another TTS.
- Stock footage is from Pexels (free license). Don't point this at other people's videos.
