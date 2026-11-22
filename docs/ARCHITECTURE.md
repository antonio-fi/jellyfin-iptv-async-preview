# Live TV: asynchronous channel thumbnail previews without blocking playback

## Summary

I'd like to propose an opt-in Live TV guide feature that captures lightweight preview stills from IPTV streams and displays them directly in the existing **On Now** programme cards. The key design goal is to keep network waiting independent of video decoding and, critically, independent of Jellyfin's navigation and playback.

This proposal is based on an experimental, **unofficial** extension tested by a Jellyfin 10.11.11 user on a local Docker/Linux installation. It is not a patch against the Jellyfin codebase and should not be treated as production-ready or as a confirmed core Jellyfin bug fix.

## User experience

- When the **On Now** view opens, first available still images progressively populate programme cards.
- The station/channel name appears in a compact badge at the top-left of each card, even before a still has arrived.
- Moving the mouse over a card refreshes that card's preview using newly captured frames. Moving away leaves the latest visible still in place.
- Opening a channel for playback must never wait for any thumbnail fetch, decoder job or preview-related API response.
- If preview acquisition fails, leave the original Jellyfin card artwork intact.

## Technical approach worth exploring

1. Use a shared asynchronous HTTP client to manage many **logical HLS network sessions** concurrently. Waiting for requests should not require a dedicated worker thread or FFmpeg process per channel.
2. Store per-channel receiver state, the most recent JPEG and update timestamps in a channel-keyed registry.
3. Pass completed HLS segment downloads to a *bounded* video-decoding queue. Only the decoders need a concurrency limit; network I/O does not need to be serialized behind them.
4. Keep the Live TV guide's DOM updates asynchronous and non-blocking: register cards when they actually appear, paint cached frames immediately, and do not await a full channel-name/guide lookup before navigating.
5. Treat playback and previews as separate consumers of IPTV data. The experiment **does not** hand its preview buffer to Jellyfin's playback engine.
6. Rate-limit non-focused refreshes, prioritise a focused channel, and stop network work when the guide is no longer visible. Consider bandwidth/resource quotas and failure backoff.

## Preliminary observations (one user's setup; not benchmarks)

- A first prototype captured one still sequentially per channel. A full sweep of the user's channel list could take **more than 10 minutes**.
- After moving to multiple concurrent network sessions with a small, separate decoding pool, the user observed the guide's thumbnails become available in approximately **6–7 seconds** in one trial.
- Removing an unnecessary preview-related asynchronous dependency in the JavaScript was followed by noticeably faster channel opening (around **2 seconds** observed). This is a correlation from a local experiment, **not proof of the precise cause** or of improvements to Jellyfin itself.
- Long-running stability, bitrate impact, CPU usage, security, different HLS formats, and behaviour with restricted/encrypted streams have not been systematically validated.

## What would be helpful from maintainers

- Feedback on the most suitable extension point (web client, server-side support, optional plugin, or external service).
- Whether current Live TV APIs expose stable programme-to-channel identifiers appropriate for thumbnail mapping.
- Recommendations for handling authentication, media-source permissions, HLS variants, segment decoding, and resource quotas.
- Interest in a small proof of concept with tests and explicit feature gating, rather than a direct modification of shipped web assets.

## Credit

The asynchronous multi-session design and separation of network waiting from decoder workers were proposed by the user **Andrea**, with the implementation prototyped collaboratively using ChatGPT. Please preserve that attribution if discussing or adapting this prototype.

## Caveats

This approach is most suitable for ordinary unencrypted HLS sources. It should not be implemented as arbitrary unauthenticated HTTP endpoints or exposed without access control. A shareable implementation would need security review, configurable binding/origins, resource limits, and tests before publication as installable code.
