import re
p = r"tests/test_notification_impl.py"
with open(p, "r", encoding="utf-8") as f:
    c = f.read()
# Fix the WeCom test_send_exception: ch -> channel
old = '        channel._session = mock_session\n        assert await ch.send(make_notification()) is False\n\n    async def test_send_with_template(self, channel):\n        channel._message_template = "T: {alarm_id}"'
new = '        channel._session = mock_session\n        assert await channel.send(make_notification()) is False\n\n    async def test_send_with_template(self, channel):\n        channel._message_template = "T: {alarm_id}"'
if old in c:
    c = c.replace(old, new)
    with open(p, "w", encoding="utf-8") as f:
        f.write(c)
    print("replaced")
else:
    print("not found")
