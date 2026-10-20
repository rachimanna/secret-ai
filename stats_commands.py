"""Statistics and analytics commands for Secret AI bot.

Provides /stats command with various analytics views.
"""
import time
from telegram import Update
from telegram.ext import ContextTypes

from analytics import Analytics
from ratelimit import RateLimiter
from response_cache import ResponseCache
from user_preferences import UserPreferences
from error_handler import logger


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics and analytics."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    is_admin = update.effective_user.id in context.bot_data.get("admins", [])

    # Initialize services
    analytics = context.bot_data.get("analytics")
    cache = context.bot_data.get("cache")
    rate_limiter = context.bot_data.get("rate_limiter")
    prefs = context.bot_data.get("preferences")

    if not analytics:
        await update.message.reply_text("📊 Analytics not initialized.")
        return

    # Parse command arguments
    args = context.args
    view = args[0] if args else "user"

    # Personal stats (always available)
    if view == "user" or view == "me":
        user_stats = analytics.get_user_stats(user_id, days=30)

        msg = f"📊 **Your Statistics (last 30 days)**\n\n"
        msg += f"💬 Messages: {user_stats['total_messages']}\n"
        msg += f"⚡ Commands: {user_stats['total_commands']}\n"

        if user_stats['favorite_commands']:
            msg += f"\n**Top Commands:**\n"
            for cmd in user_stats['favorite_commands'][:3]:
                msg += f"  • {cmd['command']}: {cmd['count']}×\n"

        if user_stats['models_used']:
            msg += f"\n**Models Used:**\n"
            for model in user_stats['models_used'][:3]:
                msg += f"  • {model['model']}: {model['count']}×\n"

        # Show quota info
        if rate_limiter:
            quota_stats = rate_limiter.get_stats(chat_id, user_id)
            if quota_stats:
                reset_hours = int((quota_stats.get('reset_at', time.time()) - time.time()) / 3600) + 1
                msg += f"\n**Today's Usage:**\n"
                msg += f"  • Requests: {quota_stats.get('requests_used', 0)}\n"
                msg += f"  • Resets in: {reset_hours}h\n"

        await update.message.reply_text(msg, parse_mode=None)
        return

    # Admin-only views
    if not is_admin:
        await update.message.reply_text("🔒 Admin access required for this view.")
        return

    if view == "dashboard" or view == "all":
        dashboard = analytics.get_dashboard(hours=24)

        msg = f"📊 **Bot Dashboard (last 24h)**\n\n"
        msg += f"👥 Active Users: {dashboard['overview']['total_users']}\n"
        msg += f"💬 Messages: {dashboard['overview']['total_messages']}\n"
        msg += f"⚡ Commands: {dashboard['overview']['total_commands']}\n"
        msg += f"⚡ Avg Response: {dashboard['overview']['avg_response_time_ms']}ms\n"
        msg += f"✅ Success Rate: {dashboard['overview']['success_rate']}%\n"

        if dashboard['top_commands']:
            msg += f"\n**Top Commands:**\n"
            for cmd in dashboard['top_commands'][:5]:
                msg += f"  • {cmd['command']}: {cmd['count']}×\n"

        if dashboard['models']:
            msg += f"\n**Model Usage:**\n"
            for model in dashboard['models'][:3]:
                msg += f"  • {model['model']}\n"
                msg += f"    Requests: {model['requests']}, Cost: ${model['total_cost_usd']:.4f}\n"
                msg += f"    Cache rate: {model['cache_rate']}%\n"

        await update.message.reply_text(msg, parse_mode=None)

    elif view == "cache":
        if not cache:
            await update.message.reply_text("Cache not initialized.")
            return

        cache_stats = cache.get_stats()

        msg = f"💾 **Cache Statistics**\n\n"
        msg += f"Entries: {cache_stats['total_entries']}\n"
        msg += f"Total Hits: {cache_stats['total_hits']}\n"
        msg += f"Hit Rate: {cache_stats['hit_rate']}\n"
        msg += f"Expired: {cache_stats['expired_entries']}\n"

        if cache_stats['top_queries']:
            msg += f"\n**Most Cached Queries:**\n"
            for q in cache_stats['top_queries'][:5]:
                msg += f"  • {q['prompt'][:50]}... ({q['hits']} hits)\n"

        await update.message.reply_text(msg, parse_mode=None)

    elif view == "errors":
        error_stats = logger.get_error_stats(hours=24)

        msg = f"⚠️ **Error Statistics (last 24h)**\n\n"
        msg += f"Total Errors: {error_stats['total_errors']}\n"

        if error_stats['by_category']:
            msg += f"\n**By Category:**\n"
            for cat, count in error_stats['by_category'].items():
                msg += f"  • {cat}: {count}\n"

        if error_stats['top_error_types']:
            msg += f"\n**Top Error Types:**\n"
            for err in error_stats['top_error_types'][:5]:
                msg += f"  • {err['type']}: {err['count']}\n"

        await update.message.reply_text(msg, parse_mode=None)

    elif view == "trends":
        trends = analytics.get_trends(days=7)

        msg = f"📈 **Weekly Trends**\n\n"
        for day in trends['daily'][-7:]:
            msg += f"**{day['date']}**\n"
            msg += f"  Users: {day['users']}, Messages: {day['messages']}\n"
            msg += f"  Cost: ${day['cost_usd']:.4f}, Errors: {day['errors']}\n\n"

        await update.message.reply_text(msg, parse_mode=None)

    elif view == "help":
        msg = """📊 **Stats Command Help**

Available views:
• `/stats` or `/stats user` - Your personal statistics
• `/stats dashboard` - Bot overview (admin)
• `/stats cache` - Cache performance (admin)
• `/stats errors` - Error statistics (admin)
• `/stats trends` - Weekly trends (admin)
• `/stats help` - This help message

Examples:
  /stats
  /stats dashboard
  /stats cache
"""
        await update.message.reply_text(msg, parse_mode=None)

    else:
        await update.message.reply_text(f"Unknown view: {view}. Use `/stats help` for available options.")


async def preferences_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage user preferences."""
    user_id = update.effective_user.id
    prefs_manager = context.bot_data.get("preferences")

    if not prefs_manager:
        await update.message.reply_text("⚙️ Preferences not initialized.")
        return

    args = context.args

    # Show current preferences
    if not args:
        prefs = prefs_manager.get(user_id)

        msg = f"⚙️ **Your Preferences**\n\n"
        msg += f"Response Style: {prefs.get('response_style', 'auto')}\n"
        msg += f"Tone: {prefs.get('tone', 'friendly')}\n"
        msg += f"Language: {prefs.get('language', 'auto')}\n"
        msg += f"Emoji: {prefs.get('emoji_density', 'auto')}\n"
        msg += f"Cache: {'✅' if prefs.get('enable_cache') else '❌'}\n"
        msg += f"History: {'✅' if prefs.get('enable_history') else '❌'}\n"

        recommended_model = prefs_manager.get_recommended_model(user_id)
        if recommended_model:
            msg += f"\n💡 Recommended Model: {recommended_model}\n"

        msg += f"\nUse `/prefs set <key> <value>` to change settings."
        msg += f"\nExample: `/prefs set tone casual`"

        await update.message.reply_text(msg, parse_mode=None)
        return

    # Set preference
    if args[0] == "set" and len(args) >= 3:
        key = args[1]
        value = args[2]

        # Validate and set
        valid_settings = {
            "response_style": ["auto", "brief", "detailed"],
            "tone": ["friendly", "formal", "casual", "expert"],
            "emoji_density": ["none", "low", "auto", "high"],
            "language": None,  # Any value
        }

        if key in valid_settings:
            if valid_settings[key] and value not in valid_settings[key]:
                await update.message.reply_text(f"❌ Invalid value for {key}. Options: {', '.join(valid_settings[key])}")
                return

            prefs_manager.set(user_id, **{key: value})
            await update.message.reply_text(f"✅ Updated {key} to {value}")
        else:
            await update.message.reply_text(f"❌ Unknown preference: {key}")

    elif args[0] == "reset":
        prefs_manager.reset(user_id)
        await update.message.reply_text("✅ Preferences reset to defaults.")

    else:
        await update.message.reply_text("Usage: `/prefs` or `/prefs set <key> <value>`")
