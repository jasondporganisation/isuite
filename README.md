# iSuite (open edition)

iSuite is a small Telegram bot that helps you keep track of the people you meet and remember to follow up with them.

You run it yourself, on your own computer. You use your own Telegram bot and your own AI key. Your contacts are stored on your own computer, not on anyone else's server.

The text and photos you send still pass through Telegram, and they are processed by Anthropic's AI service, which is outside Singapore. NOTICE.md explains what that means for you. Please read it.

## What it does

- You send it a photo of a sign-up sheet or name card. It reads the names and numbers.
- You can also forward a contact, or type `lead: Name 91234567`.
- It shows you what it found. You tap "Looks right" before anything is saved.
- You mark whether each person agreed to be contacted.
- Every morning from 08:30 (Singapore time) it sends you a short list of who to follow up with.
- For each person who agreed to be contacted, it writes a short WhatsApp message for you to review.
- It reminds you again after 3, 7 and 14 days if you have not heard back.
- You tap Sent, Replied, Booked or Not keen to keep the list up to date.

## What it does not do

- **It does not send messages to your clients.** It only writes a draft and gives you a link. You open WhatsApp, read the draft, change it if you like, and press send yourself.
- It does not list people you recorded as not agreeing to be contacted.
- It does not give advice on products, prices or returns.
- It removes ID numbers and policy numbers before it stores or sends anything. It cannot catch every way of writing them, so please do not type them in.

It is made for Singapore phone numbers and Singapore time.

## What you need

- A computer that can stay on while the bot runs. A Mac is fine.
- Python 3.10 or newer is expected to work. It has been tested on Python 3.14 on a Mac, and not on older versions. A recent Mac may already have Python.
- A Mac is what it was tested on. Linux is expected to work. Windows is not tested and not supported, because it needs extra time zone data that this tool does not install.
- A Telegram account on your phone.
- An Anthropic API key. This is separate from any Claude chat subscription. It costs money, you pay for it, and you can set a spending limit.

Nothing else needs to be installed.

## Set up, step by step

### 1. Check Python

Open the Terminal app (on a Mac, press Command and Space, type Terminal, press Return). Type this and press Return:

    python3 --version

If it says 3.10 or higher, carry on. If not, install Python from python.org.

### 2. Get the code

Download the code as a zip file from the page where you found it, and unzip it. Then in Terminal, go into the folder. For example, if it is in your Downloads folder:

    cd ~/Downloads/isuite

(Use the real folder name you see.)

### 3. Try it without any keys

This plays a pretend day so you can see what the bot does. It needs no keys and no internet.

    python3 -m isuite.run --dry-run

### 4. Make your Telegram bot

1. In Telegram, search for BotFather. It has a blue tick.
2. Send it `/newbot`.
3. Choose a name, then a username that ends in `bot`.
4. BotFather replies with a long token. Keep this private. Anyone with it can control your bot.

### 5. Find your Telegram id

Your id is a number. One way to see it is to send any message to a bot such as userinfobot in Telegram. It replies with your id. (That is someone else's bot, so only use it for this.)

### 6. Get an Anthropic API key

1. Go to the Anthropic Console (console.anthropic.com) and sign in or create an account.
2. Add a small amount of credit.
3. Set a monthly spending limit if the Console offers one.
4. Create an API key and copy it. Keep it private.

### 7. Save your three answers

    python3 -m isuite.setup

It asks for the bot token, your Telegram id and the API key. The token and key are hidden as you type. They are saved in a private file in your home folder, in a folder called `.isuite`. It is set so that only your user account on the computer can read it.

### 8. Start the bot

    python3 -m isuite.run

Leave this window open. In Telegram, open your bot and send `/start`. It will welcome you. It is set up so that it answers only you. Anyone else who finds it is told it is a private copy.

### 9. Keep it running

The bot only works while this window is open and the computer is awake.

- On a Mac, you can stop it from sleeping while it runs by starting it like this instead: `caffeinate -i python3 -m isuite.run`
- Run it on one computer only.
- If you close the window or shut the lid, the bot stops. Start it again the same way. Your saved contacts are kept in the database file.

## Daily use

- Send a photo, forward a contact, or type `lead: Name 91234567`.
- Check the list and tap "Looks right".
- Tap "Consent given" for people who agreed to be contacted.
- In the morning, open the WhatsApp link, read the draft, and send it yourself.
- Tap Sent, Replied, Booked or Not keen.
- Type `roadshow <where>` on days you are out meeting people. The bot reminds you the next day if you added nobody.
- `/today` shows today's list at any time.
- `/week` shows your numbers for the week.

## Take your data with you

Send `/export` to the bot. It sends back your list of names, numbers and stages. Copy it somewhere safe.

## Stop the bot

Click the Terminal window and press Control and C.

## Delete your data

1. Stop the bot.
2. Delete the `.isuite` folder in your home folder. It holds your keys and your contacts. (If you set a different database path, delete that file too.)
3. In Telegram, send BotFather `/revoke` to cancel the bot token, or `/deletebot` to remove the bot.
4. In the Anthropic Console, delete the API key.
5. In Telegram, delete the chat with your bot to clear the messages on your phone.

## If something goes wrong

- **"ISUITE_TELEGRAM_TOKEN is not set":** run `python3 -m isuite.setup` again.
- **"the config file can be read by other users":** run the `chmod 600` command it prints.
- **The bot does not answer:** check the Terminal window is still open, and that the computer is awake and online.
- **"I can't read leads right now":** the AI service did not answer. Wait a minute and try again. Check that your Anthropic account has credit.
- **It says "private copy":** the Telegram id you saved is not the account you are messaging from. Run setup again with the right id.
- **A photo cannot be read:** type it instead: `lead: Name 91234567`.
- **You changed the bot token:** delete the database file in the `.isuite` folder, or the bot will wait for old messages.

Please read NOTICE.md before you use this with real client details.

## For developers

Python standard library only. Run the tests with:

    python3 -W error -m unittest discover -s tests

To add your own company's name to the list of words a draft must never contain, see `isuite/house_terms.py`.
