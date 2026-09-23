import os
import requests

token = os.environ["BUFFER_TOKEN"]
channel_id = os.environ["BUFFER_CHANNEL_ID"]

query = """
mutation {
createPost(
input: {
text: "Test post from GitHub Actions"
channelId: "%s"
schedulingType: automatic
mode: addToQueue
}
) {
... on PostActionSuccess {
post {
id
}
}

... on MutationError {
message
}
}
}
""" % channel_id

response = requests.post(
"https://api.buffer.com/graphql",
headers={
"Authorization": f"Bearer {token}",
"Content-Type": "application/json",
},
json={"query": query},
)

print(response.status_code)
print(response.text)
