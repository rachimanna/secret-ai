"""The owner's numbers: people, activity, groups, money. Counts only, nobody's messages."""
import time

def owner_stats(data, now=None):
    now = int(now or time.time())
    day, week = now - 86400, now - 7 * 86400
    db, groups = data["memory"].db, data["groups"].db
    one = lambda sql, args=(): db.execute(sql, args).fetchone()[0] or 0
    firsts = "SELECT user_id, MIN(created) AS first FROM messages WHERE role='user' AND created IS NOT NULL GROUP BY user_id"
    top = groups.execute(
        "SELECT COALESCE(NULLIF(i.title, ''), 'группа ' || m.chat_id), COUNT(*) FROM group_messages m "
        "LEFT JOIN group_info i USING(chat_id) WHERE m.created>? GROUP BY m.chat_id ORDER BY 2 DESC LIMIT 5", (week,)).fetchall()
    return {
        "users": one("SELECT COUNT(*) FROM (SELECT user_id FROM messages UNION SELECT user_id FROM settings)"),
        "new_week": one(f"SELECT COUNT(*) FROM ({firsts}) WHERE first>?", (week,)),
        "active_day": one("SELECT COUNT(DISTINCT user_id) FROM messages WHERE role='user' AND created>?", (day,)),
        "active_week": one("SELECT COUNT(DISTINCT user_id) FROM messages WHERE role='user' AND created>?", (week,)),
        "messages_week": one("SELECT COUNT(*) FROM messages WHERE role='user' AND created>?", (week,)),
        "groups": groups.execute("SELECT COUNT(*) FROM group_info").fetchone()[0] or 0,
        "groups_active": groups.execute("SELECT COUNT(DISTINCT chat_id) FROM group_messages WHERE created>?", (week,)).fetchone()[0] or 0,
        "top_groups": [{"title": title, "messages": count} for title, count in top],
        "pictures_today": one("SELECT SUM(photos) FROM gen_daily WHERE day=?", (time.strftime("%Y-%m-%d", time.gmtime(now)),)),
        "stars": one("SELECT SUM(stars) FROM gen_payments"),
        "payments": one("SELECT COUNT(*) FROM gen_payments"),
        "invited": one("SELECT COUNT(*) FROM referrals"),
        "sales": data["memory"].subs.numbers(now),
    }


def stats_text(stats):
    groups = "\n".join(f"{i}. {item['title'][:40]} · {item['messages']}" for i, item in enumerate(stats["top_groups"], 1))
    return (f"📊 **Статистика Secret AI**\n\n"
            f"**Люди**\n- всего: {stats['users']}\n- новых за неделю: {stats['new_week']}\n"
            f"- активных за сутки: {stats['active_day']} · за неделю: {stats['active_week']}\n"
            f"- сообщений за неделю: {stats['messages_week']}\n- пришли по приглашению: {stats['invited']}\n\n"
            f"**Группы**\n- всего: {stats['groups']} · активных за неделю: {stats['groups_active']}\n"
            + (f"\nСамые живые за неделю:\n{groups}\n" if groups else "") +
            f"\n**Генерации и оплаты**\n- бесплатных картинок сегодня: {stats['pictures_today']}\n"
            f"- оплат: {stats['payments']} · звёзд: {stats['stars']} ⭐\n\n"
            f"**Подписка Pro за неделю**\n- упёрлись в лимит: {stats['sales']['limit_hit_week']} чел.\n"
            f"- начали пробный период: {stats['sales']['trials_week']}\n"
            f"- купили: {stats['sales']['bought_week']} ({stats['sales']['conversion']}%) · продлили: {stats['sales']['renewed_week']}\n"
            f"- звёзд с подписок: {stats['sales']['stars_week']} ⭐\n"
            f"- сейчас активно: людей {stats['sales']['pro_active']} · групп {stats['sales']['groups_active']}")
