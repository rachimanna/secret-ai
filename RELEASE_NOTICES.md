# Automatic update announcements

A production bot reads release_notes.json after successful post_init. Each release has an increasing integer version and a plain-text message. Updating the release file is part of development (see AGENTS.md); the owner needs no command, text input or extra API key. This first version announces the features included in PR #17.

Recipients are saved private chat IDs and group IDs. Existing message/settings records, group_info and /start welcome markers are backfilled. A new tracker records future /start-only users and supported group chats; it excludes business messages, channels, bots and disallowed users. Group members are never enumerated or messaged individually. Each group receives one message.

SQLite stores a frozen recipient list and per-chat delivery state per version in memory.db. Ordinary restarts resume pending deliveries; delivered and permanently unavailable chats are skipped. Lower versions after rollback are ignored; a newer release supersedes pending older notices. New users after a batch was created start receiving news with the next release. Manual /broadcast remains independent.

Announcements run in the background, after startup completes. The separate SECRET_AI_TEST bot never stages or delivers them. /test pauses the announcement worker; turning /test off resumes it automatically. ALLOWED_USERS is respected for private deliveries.

Telegram rate-limit waits apply across the announcement worker; transient failures get up to five leased attempts with backoff. Blocked or unavailable chats are skipped, and group-to-supergroup migration is followed. Delivery progress is included in normal snapshots, with a checkpoint before the batch and after every delivery when DATABASE_URL is configured. A failed checkpoint pauses sends until persistence recovers. Run one production bot per database; separate replicas with separate SQLite files cannot coordinate leases.

As with other Telegram outboxes, exact-once delivery cannot be guaranteed if the process crashes after Telegram accepts a message but before its acknowledgement is committed, or a historical database backup is restored. A local persistent disk or DATABASE_URL is needed for progress to survive machine replacement. No notifications are sent during tests or merely by opening/merging a pull request; they start when this code and manifest run in production.

An explicit editorial correction can set correct_text: true while retaining the same version. Only pending sends use the corrected text; the frozen audience and delivered receipts are preserved. Previously delivered Telegram messages are not edited. Remove this flag when preparing the next new release.
