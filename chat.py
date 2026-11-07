import os, requests

MODEL = "openai/gpt-4o-mini"  # swap for any model ID from openrouter.ai/models
KEY = os.getenv("api_key")
SYSTEM_PROMPT = open("system_prompt.md").read()

# The model remembers nothing, so we resend the full history every turn
messages = [{"role": "system", "content": SYSTEM_PROMPT}]

print(f"Chatting with {MODEL} + your system prompt. Type 'quit' to exit.\n")
while True:
    user = input("You: ")
    if user.strip().lower() == "quit":
        break
    messages.append({"role": "user", "content": user})

    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"model": MODEL, "messages": messages},
    )
    data = r.json()
    if "choices" not in data:
        print("Error:", data)
        messages.pop()  # drop the failed turn
        continue

    reply = data["choices"][0]["message"]["content"]
    messages.append({"role": "assistant", "content": reply})
    print(f"\nModel: {reply}\n")
