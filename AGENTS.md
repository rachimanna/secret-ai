# Secret AI maintenance

For every user-visible release, update release_notes.json in the same change:
- Increase the positive integer version (YYYYMMDDNN convention); never reuse an already published version.
- Write concise Russian plain-text release notes covering the actual shipped features and fixes, within 3900 UTF-16 units.
- Do not include secrets, internal review text, test-only features, or claims of deployment before it happens.
The production bot automatically sends this file's text to its saved private chats and groups after successful startup. The owner does not need to run /broadcast or write release text. Keep the manual /broadcast workflow available.
Run the release-notice regression tests and the existing verification workflow. Tests must use fake Telegram delivery; never broadcast to real users for testing.

For an explicit owner-requested wording correction to an existing announcement, keep its version and set correct_text to true in release_notes.json. This corrects pending deliveries without sending the same release again to delivered chats. Remove the flag for the next new release. User-facing announcements should describe features and fixes; do not mention internal broadcast commands or owner delivery controls.
