# Privacy Policy

**Effective:** 10 November 2026

This policy covers the GTA 6 clips pipeline in this repository
(<https://github.com/Manas236/GTA6-Creativity>) and the Meta app it uses. One person, the owner of
that GitHub account, runs both.

## What this is

An automated tool. It finds popular public GTA 6 videos on YouTube and Twitch, cuts a short clip
from each, adds a text hook, a voiceover and captions, and posts the clip as a Reel to **one**
Instagram account and **one** Facebook Page. Each post credits the original creator. The operator
approves clips in a private Telegram chat.

It is not a website or a consumer app. Nobody else can log in to it.

## What it stores

All of it is kept in a local database (`pipeline.db`) and a `media/` folder on the operator's
computer, or on a server the operator rents.

**About public videos and their creators** (from YouTube and Twitch)
- The video's ID and URL, title, length, view/comment counts and upload time.
- The creator's **public** channel name and channel ID, which are used for the credit line.
- A transcript of the speech in the video, made on the operator's machine.
- Perceptual frame fingerprints (one 64-bit number per sampled frame, not an image). These are used
  to avoid posting the same footage twice.
- The generated hook, voiceover script and caption.
- The video file, the cut and the rendered clips (see the retention table below).

**About the operator's own Instagram account and Facebook Page** (data from Meta's API)
- The account and Page IDs, the Instagram username, the Page name and the daily publishing limit.
- For each post: its ID, link, caption, publish status and time.
- Totals for each post (views, likes, comments, shares, saves, reach) at 24 and 72 hours. These
  are aggregate counts only.
- The API access tokens. They are kept in a `.env` file on that machine, never in this repository.

**About the operator's Telegram chat**
- The chat ID, the IDs of the approval messages and which button was tapped. Taps from any other
  chat are ignored.

**What it does not collect**
- Nothing about viewers, followers or commenters: no names, no comment text, no messages, no
  follower lists.
- No cookies, no website, no tracking and no advertising.

## Where data is sent

| Service | What it receives | Why |
|---|---|---|
| YouTube, Twitch | Search and download requests for public videos | Finding and fetching source videos |
| Google Gemini API | 4 still frames from a clip, the source video's title and the transcript of the clip | Writing the hook and checking it matches the clip |
| Meta (Instagram, Facebook) | The rendered clip and its caption; read requests for the post status and the totals above | Publishing and measuring the operator's own posts |
| Telegram | The rendered clips, hook, source and creator name, sent to the operator's own private chat | Approval and failure alerts |
| Healthchecks.io (optional) | An empty "still running" ping | Monitoring |

On Gemini's free tier, Google's terms let Google use submitted content to improve its products, and
human reviewers may read it. Everything sent there comes from publicly posted videos. Transcription,
voiceover and video rendering run locally (faster-whisper, Kokoro, ffmpeg).

Data is **never sold**, never used for advertising and not shared with anyone beyond the table
above.

## How long it is kept

| Data | Kept for |
|---|---|
| The downloaded source video | Deleted as soon as the clip is rendered |
| The cut, voiceover, subtitles and rendered clips | Deleted 7 days after the post goes live |
| All media of a rejected or failed clip | Deleted after 3 days |
| Database records (metadata, transcripts, captions, post IDs, totals, fingerprints) | Until the operator deletes them. There is no automatic expiry |
| The published Reels | Until they are deleted from Instagram / Facebook |

## Data deletion

**If you made a video that was clipped**, and you want the clip taken down or the data about it
deleted, send a direct message to the Instagram account or Facebook Page that posted it. You can
also open an issue at <https://github.com/Manas236/GTA6-Creativity/issues>. Issues are public, so
don't include anything private.

Within 30 days, the operator will:
1. delete the post from Instagram and Facebook;
2. delete every file made from your video;
3. delete the stored transcript, title, hook, caption, post records and totals for it;
4. reply to confirm.

To make sure your video is never picked again, the pipeline keeps only its source ID and its frame
fingerprints, marked as excluded. If you want those deleted as well, say so. Your video could then
be selected again in future.

**If you are the account holder who connected the Meta app**, here is how to revoke its access:
1. On Facebook, go to **Settings & privacy → Settings → Business integrations** (or **Apps and
   websites**).
2. Remove the app. Its tokens stop working immediately.
3. Delete the `.env` file and `pipeline.db` from the machine running the pipeline. That removes
   everything listed under "About the operator's own Instagram account and Facebook Page".

**Anyone else:** the pipeline holds no data about you.

## Contact

Send a direct message to the Instagram account or Facebook Page that posts the clips, or open an
issue at <https://github.com/Manas236/GTA6-Creativity/issues>.

## Changes

This file describes what the code in this repository actually does. If the code changes what it
stores or sends, this file is updated in the same commit. The history of every version is in the
repository's git log.
