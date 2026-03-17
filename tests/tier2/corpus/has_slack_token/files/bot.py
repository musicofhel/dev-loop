from slack_sdk import WebClient

SLACK_BOT_TOKEN = "xoxb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"

def post_message(channel, text):
    client = WebClient(token=SLACK_BOT_TOKEN)
    client.chat_postMessage(channel=channel, text=text)
