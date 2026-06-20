"""Translate WeChat built-in emoticon *codes* into the closest Unicode emoji.

WeChat renders its own little smiley/object images inline with text, but in the
underlying message body those images are stored as bracketed *codes* such as
``[捂脸]`` or its English alias ``[Facepalm]``.  When we export plain text /
HTML / CSV those codes show up as literal characters instead of a picture.

This module maps every common WeChat emoticon code (the official Chinese names
*and* the English aliases WeChat ships in its other locales) to the nearest
standard Unicode emoji, so an exported conversation reads like emoji rather than
``[呲牙]`` noise.

Public surface
--------------
``EMOJI_MAP``
    ``Dict[str, str]`` mapping a full bracketed code (keys *include* the square
    brackets, e.g. ``"[捂脸]"``) to a single Unicode emoji string.

``replace_emoji(text)``
    Replace every *known* ``[code]`` occurrence in ``text`` with its emoji.
    Unknown ``[...]`` spans (custom stickers, references we do not recognise)
    are left exactly as-is.  A single pre-compiled regex over the known keys is
    used, so this is cheap to call on every message.

Notes
-----
* WeChat's English shortcode names are *not* a published standard and have
  drifted between client versions; the aliases here are the widely-circulated
  ones (Smile / Grimace / Drool / Scowl / CoolGuy / Facepalm / Joyful ...).  A
  handful of codes map to the same emoji (e.g. several "angry" faces) on
  purpose -- Unicode simply has no 1:1 counterpart for every WeChat drawing.
* Codes WeChat has no faithful Unicode equivalent for (purely animated poses
  like ``[跳跳]``/``[左太极]``) are still mapped to the *closest* emoji so the
  text never regresses to a bare ``[code]``.
* Keys are matched case-sensitively for the Chinese names (there is only one
  form) but we additionally register a few lowercase English variants WeChat is
  known to emit (``[OK]`` vs ``[ok]``).

Sources cross-referenced while compiling this table:
* qiuyinghua/wechat-emoticons ``encode-mapping.json`` (canonical English names)
* Crissov/unicode-proposals issue #359 (WeChat/QQ -> Unicode suggestions)
* Emojipedia "WeChat" platform page and assorted bilingual reference tables.

stdlib only; no third-party dependencies.
"""
from __future__ import annotations

import re
from typing import Dict, List


# --------------------------------------------------------------------------- #
# The map.  Keys INCLUDE the surrounding square brackets.
#
# Organised in loose groups (faces, gestures, hearts, objects, nature, newer
# "social" emoticons) purely for human readability -- order does not matter to
# the matcher.  Where WeChat ships an English alias for a code it is listed
# right after the Chinese one and points at the same emoji.
# --------------------------------------------------------------------------- #
EMOJI_MAP: Dict[str, str] = {
    # ---- classic faces --------------------------------------------------- #
    "[微笑]": "😊", "[Smile]": "😊",
    "[撇嘴]": "😖", "[Grimace]": "😖",
    "[色]": "😍", "[Drool]": "😍",
    "[发呆]": "😳", "[Scowl]": "😳",
    "[得意]": "😎", "[CoolGuy]": "😎", "[Chill]": "😎",
    "[流泪]": "😢", "[Sob]": "😢",
    "[害羞]": "☺️", "[Shy]": "☺️",
    "[闭嘴]": "🤐", "[Shutup]": "🤐", "[Silent]": "🤐",
    "[睡]": "😴", "[Sleep]": "😴",
    "[大哭]": "😭", "[Cry]": "😭",
    "[尴尬]": "😅", "[Awkward]": "😅",
    "[发怒]": "😡", "[Angry]": "😡", "[Pout]": "😡",
    "[调皮]": "😜", "[Tongue]": "😜",
    "[呲牙]": "😁", "[Grin]": "😁", "[Toothy]": "😁",
    "[惊讶]": "😮", "[Surprise]": "😮", "[Surprised]": "😮",
    "[难过]": "🙁", "[Frown]": "🙁", "[Sad]": "🙁",
    "[酷]": "😎", "[Cool]": "😎", "[Ruthless]": "😎",
    "[冷汗]": "😓", "[Blush]": "😓", "[ColdSweat]": "😓",
    "[抓狂]": "😫", "[Scream]": "😫", "[Crazy]": "😫",
    "[吐]": "🤮", "[Puke]": "🤮", "[Vomit]": "🤮",
    "[偷笑]": "😏", "[Chuckle]": "😏", "[Hehe]": "😏",
    "[愉快]": "😊", "[Joyful]": "😊", "[Happy]": "😊",
    "[白眼]": "🙄", "[Slight]": "🙄", "[RollEyes]": "🙄",
    "[傲慢]": "😤", "[Smug]": "😤", "[Proud]": "😤",
    "[饥饿]": "😋", "[饿]": "😋", "[Hungry]": "😋",
    "[困]": "😪", "[Drowsy]": "😪", "[Tired]": "😪",
    "[惊恐]": "😱", "[Panic]": "😱", "[Scared]": "😱",
    "[流汗]": "😓", "[Sweat]": "😓", "[Sweating]": "😓",
    "[憨笑]": "😄", "[Laugh]": "😄", "[Grateful]": "😄",
    "[悠闲]": "😌", "[Commando]": "😌", "[Loafer]": "😌", "[Relaxed]": "😌",
    "[奋斗]": "💪", "[Determined]": "💪", "[Strive]": "💪",
    "[咒骂]": "🤬", "[Scold]": "🤬", "[Scolding]": "🤬",
    "[疑问]": "❓", "[Shocked]": "❓", "[Doubt]": "❓",
    "[嘘]": "🤫", "[Shhh]": "🤫", "[Hush]": "🤫",
    "[晕]": "😵", "[Dizzy]": "😵",
    "[衰]": "😩", "[Toasted]": "😩", "[BadLuck]": "😩",
    "[骷髅]": "💀", "[Skull]": "💀",
    "[折磨]": "😖", "[Tormented]": "😖",
    "[抠鼻]": "🤥", "[NosePick]": "🤥", "[Nosepick]": "🤥",
    "[鼓掌]": "👏", "[Clap]": "👏", "[Applause]": "👏",
    "[糗大了]": "😱", "[Shame]": "😱", "[Embarrassed]": "😱",
    "[坏笑]": "😈", "[Trick]": "😈", "[BadSmile]": "😈",
    "[左哼哼]": "😤", "[Bah!L]": "😤", "[BahL]": "😤",
    "[右哼哼]": "😤", "[Bah!R]": "😤", "[BahR]": "😤",
    "[哈欠]": "🥱", "[Yawn]": "🥱",
    "[鄙视]": "😒", "[Pooh-pooh]": "😒", "[Lookdown]": "😒", "[Despise]": "😒",
    "[委屈]": "🥺", "[Shrunken]": "🥺", "[Wronged]": "🥺", "[Grievance]": "🥺",
    "[快哭了]": "😣", "[TearingUp]": "😣", "[Puling]": "😣",
    "[阴险]": "😏", "[Sly]": "😏", "[Cunning]": "😏",
    "[亲亲]": "😘", "[Kiss]": "😘",
    "[吓]": "😨", "[Wrath]": "😨", "[Fright]": "😨",
    "[可怜]": "🥺", "[Whimper]": "🥺", "[Pitiful]": "🥺",
    "[笑脸]": "😄", "[Smirk]": "😄",
    "[生病]": "😷", "[Sick]": "😷", "[Mask]": "😷",
    "[脸红]": "😊", "[Flushed]": "😊", "[Hellsing]": "😊",
    "[破涕为笑]": "😂", "[笑哭]": "😂", "[Lol]": "😂", "[Laughcry]": "😂", "[喜极而泣]": "😂",
    "[恐惧]": "😨", "[Terror]": "😨",
    "[失望]": "😞", "[Let Down]": "😞", "[LetDown]": "😞", "[Disappointed]": "😞",
    "[无语]": "😑", "[Speechless]": "😑",
    "[嘿哈]": "🤣", "[Hey]": "🤣",
    "[捂脸]": "🤦", "[Facepalm]": "🤦", "[Covering Face]": "🤦",
    "[奸笑]": "😜", "[Smirk2]": "😜", "[Smirking]": "😜",
    "[机智]": "🤓", "[Smart]": "🤓",
    "[皱眉]": "😟", "[Concerned]": "😟", "[Frowning]": "😟",
    "[耶]": "✌️", "[Yeah!]": "✌️", "[Yeah]": "✌️",

    # ---- newer "social" faces ------------------------------------------- #
    "[吃瓜]": "🍉", "[Onlooker]": "🍉", "[Watching]": "🍉",
    "[加油]": "💪", "[Add Oil]": "💪", "[AddOil]": "💪", "[Cheer]": "💪",
    "[汗]": "😓", "[Sweats]": "😓",
    "[天啊]": "😱", "[OMG]": "😱",
    "[Emm]": "🤨", "[emm]": "🤨", "[Emmm]": "🤨",
    "[社会社会]": "🙏", "[Respect]": "🙏",
    "[旺柴]": "🐶", "[Doge]": "🐶",
    "[好的]": "👌", "[NoProb]": "👌", "[OK啦]": "👌",
    "[打脸]": "👋", "[Slap]": "👋",
    "[哇]": "😲", "[Wow]": "😲",
    "[翻白眼]": "🙄", "[Boring]": "🙄",
    "[666]": "🙌", "[Awesome]": "🙌", "[Sixsixsix]": "🙌",
    "[让我看看]": "👀", "[LetMeSee]": "👀", "[Let Me See]": "👀",
    "[叹气]": "😮‍💨", "[Sigh]": "😮‍💨",
    "[苦涩]": "😣", "[Hurt]": "😣", "[Bitter]": "😣",
    "[裂开]": "🫠", "[Broken]": "🫠", "[Split]": "🫠",
    "[嗯]": "😐", "[Hmm]": "😐",
    "[啊]": "😮", "[Ah]": "😮",
    "[摸鱼]": "🐟", "[Salted Fish]": "🐟", "[SaltedFish]": "🐟",

    # ---- gestures -------------------------------------------------------- #
    "[强]": "👍", "[ThumbsUp]": "👍", "[Strong]": "👍",
    "[弱]": "👎", "[ThumbsDown]": "👎", "[Weak]": "👎",
    "[握手]": "🤝", "[Shake]": "🤝", "[Handshake]": "🤝",
    "[胜利]": "✌️", "[V]": "✌️", "[Victory]": "✌️", "[Peace]": "✌️",
    "[抱拳]": "🙏", "[Salute]": "🙏", "[Fist&Palm]": "🙏", "[Worship]": "🙏",
    "[勾引]": "👈", "[Beckon]": "👈",
    "[拳头]": "✊", "[Fist]": "✊",
    "[差劲]": "🤙", "[Bad]": "🤙", "[Pinky]": "🤙",
    "[爱你]": "🤟", "[Love You]": "🤟", "[LoveYou]": "🤟", "[ILoveYou]": "🤟", "[RockOn]": "🤘",
    "[NO]": "🙅", "[No]": "🙅", "[no]": "🙅",
    "[OK]": "👌", "[ok]": "👌", "[Ok]": "👌",
    "[敬礼]": "🫡",

    # ---- hearts / love --------------------------------------------------- #
    "[爱心]": "❤️", "[Heart]": "❤️", "[Love]": "❤️", "[红心]": "❤️",
    "[心碎]": "💔", "[Broken Heart]": "💔", "[BrokenHeart]": "💔", "[分手]": "💔",
    "[拥抱]": "🤗", "[Hug]": "🤗",
    "[亲嘴]": "💋", "[BlowKiss]": "💋", "[Blow Kiss]": "💋", "[飞吻]": "💋",
    "[爱情]": "💑", "[In Love]": "💑", "[InLove]": "💑", "[Lovers]": "💑",
    "[嘴唇]": "👄", "[Lips]": "👄", "[ShowLove]": "👄",

    # ---- food / drink ---------------------------------------------------- #
    "[西瓜]": "🍉", "[Watermelon]": "🍉",
    "[啤酒]": "🍺", "[Beer]": "🍺",
    "[咖啡]": "☕", "[Coffee]": "☕",
    "[饭]": "🍚", "[米饭]": "🍚", "[Rice]": "🍚", "[Meal]": "🍚",
    "[蛋糕]": "🎂", "[Cake]": "🎂",
    "[糖]": "🍬", "[Candy]": "🍬",
    "[冰淇淋]": "🍦", "[IceCream]": "🍦",

    # ---- nature / objects ------------------------------------------------ #
    "[玫瑰]": "🌹", "[Rose]": "🌹",
    "[凋谢]": "🥀", "[Wilt]": "🥀", "[Fade]": "🥀", "[Withered]": "🥀",
    "[太阳]": "☀️", "[Sun]": "☀️",
    "[月亮]": "🌙", "[Moon]": "🌙",
    "[星星]": "⭐", "[Star]": "⭐",
    "[闪电]": "⚡", "[Lightning]": "⚡",
    "[礼物]": "🎁", "[Gift]": "🎁", "[Present]": "🎁",
    "[红包]": "🧧", "[RedPacket]": "🧧", "[Packet]": "🧧",
    "[鞭炮]": "🧨", "[Firecracker]": "🧨",
    "[烟花]": "🎆", "[Fireworks]": "🎆",
    "[炸弹]": "💣", "[Bomb]": "💣",
    "[刀]": "🔪", "[菜刀]": "🔪", "[Knife]": "🔪", "[Cleaver]": "🔪",
    "[匕首]": "🗡️", "[Dagger]": "🗡️",
    "[便便]": "💩", "[Shit]": "💩", "[Poop]": "💩",
    "[篮球]": "🏀", "[Basketball]": "🏀",
    "[足球]": "⚽", "[Soccer]": "⚽", "[Football]": "⚽",
    "[乒乓]": "🏓", "[PingPong]": "🏓", "[Pingpong]": "🏓",
    "[猪头]": "🐷", "[Pig]": "🐷",
    "[瓢虫]": "🐞", "[Ladybug]": "🐞",
    "[钱]": "💰", "[Money]": "💰",
    "[药]": "💊", "[Pill]": "💊", "[Drug]": "💊",
    "[灯泡]": "💡", "[Bulb]": "💡", "[Idea]": "💡",
    "[钟]": "🕛", "[Clock]": "🕛",
    "[勾]": "✅", "[OK手]": "✅",
    "[叉]": "❌", "[Cross]": "❌",
    "[麦克风]": "🎤", "[Microphone]": "🎤",
    "[话筒]": "🎤",
    "[音乐]": "🎵", "[Music]": "🎵",
    "[喝彩]": "🎉", "[Cheers]": "🎉", "[Celebrate]": "🎉",
    "[企鹅]": "🐧", "[Penguin]": "🐧",
    "[飞机]": "✈️", "[Plane]": "✈️", "[Airplane]": "✈️",
    "[车]": "🚗", "[Car]": "🚗",
    "[祈祷]": "🙏", "[Pray]": "🙏",
    "[庆祝]": "🥳", "[Party]": "🥳",
}


# --------------------------------------------------------------------------- #
# Compiled matcher.
#
# We build ONE alternation regex out of every known key, longest first so that
# (hypothetically overlapping) codes prefer the most specific match, and
# re.escape each key so the literal ``[`` / ``]`` (and any regex-special
# characters inside a code such as ``!`` ``?`` ``(`` ``)`` ``*`` ``&`` ``+``)
# are treated literally rather than as regex metacharacters.
# --------------------------------------------------------------------------- #
def _build_pattern(keys: List[str]) -> "re.Pattern[str]":
    # Sort longest-first; this makes the alternation deterministic when one
    # code's text is a prefix of another (re alternation is first-match, not
    # longest-match).
    ordered = sorted(keys, key=len, reverse=True)
    alternation = "|".join(re.escape(k) for k in ordered)
    return re.compile(alternation)


_PATTERN = _build_pattern(list(EMOJI_MAP.keys()))


def replace_emoji(text: str) -> str:
    """Replace every known ``[code]`` in *text* with its Unicode emoji.

    Unknown bracketed spans (custom stickers, unrecognised codes) are left
    untouched.  ``None``/empty input is returned unchanged.

    >>> replace_emoji("good morning [呲牙][玫瑰]")
    'good morning 😁🌹'
    >>> replace_emoji("[捂脸] really?  [SomethingWeDontKnow]")
    '🤦 really?  [SomethingWeDontKnow]'
    """
    if not text:
        return text
    return _PATTERN.sub(lambda m: EMOJI_MAP[m.group(0)], text)


__all__ = ["EMOJI_MAP", "replace_emoji"]


if __name__ == "__main__":  # tiny smoke test / manual sanity check
    samples = [
        "你好 [微笑] 今天 [呲牙] 真不错 [玫瑰]",
        "[捂脸][笑哭][Facepalm][Joyful]",
        "thumbs [强] / [弱] and [OK] [胜利]",
        "unknown [自定义表情] stays put",
        "",
    ]
    for s in samples:
        print(repr(s), "->", repr(replace_emoji(s)))
    print("total keys:", len(EMOJI_MAP))
