---
title: autoclip Privacy Policy
permalink: /privacy-policy/
---

# autoclip: Privacy Policy

Last updated: 2026-11-19

autoclip is a free program that runs **on your own computer**. It watches live streams you choose, cuts highlight clips, and, only
when you ask it to, uploads a clip you picked to your own YouTube, Instagram or TikTok account.

## The short version
* The developer of autoclip runs **no server** and receives **no data** from you: no account, no analytics, no tracking, no ads.
* Everything the program stores stays on your computer.
* Your TikTok login is used only to upload a clip you chose, after you confirmed it.

## What the program handles, and where it stays
| Data | Why | Where it is kept |
|---|---|---|
| Live video and public chat of the channels you enter | To find and cut highlight moments. Chat is analysed in memory to count how many different people write at once; only a short summary (counts and the most repeated phrases) is saved next to each clip. | On your computer (clips folder). A rolling video buffer is deleted when the program stops. |
| Clips, titles, descriptions you edit | So you can review and post them | On your computer |
| Login for your own TikTok / YouTube / Instagram account (access and refresh tokens) | To upload the clip you chose to your own account | A file `accounts.json` in your user settings folder on your computer. It is never shown on screen after saving and is never sent anywhere except to the platform it belongs to. |
| TikTok display name | To show which TikTok account is connected | On your computer |

## What is sent to TikTok
If you connect TikTok, the program uses **Login Kit** (scope `user.info.basic`: read your display name) and the **Content Posting API**
(scope `video.upload`: send a video to your TikTok inbox as a draft). A video, and the title/description shown in the confirmation
window, is sent to TikTok **only when you press Share and confirm**. TikTok handles that data under
[TikTok's own privacy policy](https://www.tiktok.com/legal/privacy-policy).

## Sharing
autoclip does not sell, rent or share your data with anyone. The only recipients are the platforms (TikTok, YouTube, Instagram) you
choose to upload to, at the moment you choose it.

## Your choices
* **Disconnect** in the Accounts window deletes the saved login from your computer.
* You can also remove autoclip's access in TikTok (Settings and privacy > Security and permissions > Apps and services), in your Google
  account permissions, or in Instagram's app settings.
* Deleting the autoclip folders and `%APPDATA%\autoclip` (or `~/.config/autoclip`) removes everything the program stored.

## Children
autoclip is not directed at children under 13 and does not knowingly collect their data.

## Changes and contact
If this policy changes, the new version replaces this page and the date above. Questions: open an issue at
<https://github.com/Elhour-Dev/atgxis/issues>.
