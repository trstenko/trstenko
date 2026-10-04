# Evaluation: gemini, gemini-3.5-flash-lite

Run 2026-10-04.

**41 of 44 cases passed.**

| Check | Passed |
|---|---|
| arguments | 16 of 16 |
| asks back | 4 of 4 |
| called once | 23 of 23 |
| does not say | 5 of 5 |
| length | 42 of 42 |
| local | 5 of 5 |
| no extra tool | 23 of 23 |
| says | 7 of 8 |
| tool | 41 of 42 |
| turn | 42 of 44 |

Time to the first sentence: median 0.7s, slowest 1.9s.
Requests: 46. Cost by the device's own meter: $0.0191.

## What failed

- **"do I have anything on tomorrow"** (line 23)
  - tool: called nothing
  - says: said: I can't check your calendar, since I don't have access to it.
- **"what's my dog's name"** (line 32)
  - turn: rate_limited: The AI service is busy. Try again in a moment.
- **"that's too loud"** (line 35)
  - turn: rate_limited: The AI service is busy. Try again in a moment.

## Every case

| Line | Said | Tools | Reply | Result |
|---|---|---|---|---|
| 4 | remind me to call mum at six | set_reminder | I'll remind you at six in the evening. | ok |
| 5 | remind me to take the bins out tomorrow at seven in the morning | set_reminder | I'll remind you tomorrow at seven in the morning. | ok |
| 6 | remind me at half past eight tonight to charge the bike lights | set_reminder | I'll remind you at half past eight in the evening. | ok |
| 7 | remind me on Monday at nine to email the teacher | set_reminder | I'll remind you on Monday at nine in the morning. | ok |
| 8 | remind me to water the plants in two hours | set_reminder | I'll remind you at five ten in the afternoon. | ok |
| 9 | remind me to stretch in twenty minutes | set_reminder | I'll remind you at half past three in the afternoon. | ok |
| 10 | at noon tomorrow remind me about the dentist | set_reminder | I'll remind you tomorrow at noon. | ok |
| 11 | remind me to buy milk at three | set_reminder | That time has already passed today. Did you mean three tomorrow? | ok |
| 12 | remind me to call grandma |  | When should I remind you? | ok |
| 14 | set a timer for ten minutes | set_timer | Timer set for ten minutes. | ok |
| 15 | timer for ninety seconds | set_timer | Timer set for one minute and thirty seconds. | ok |
| 16 | start a pasta timer for twelve minutes | set_timer | Timer set for twelve minutes. | ok |
| 17 | wake me up in an hour and a half | set_timer | Timer set for one hour and thirty minutes. | ok |
| 18 | can you count down five minutes for my tea | set_timer | Timer set for five minutes. | ok |
| 19 | set a timer |  | How long should I set the timer for? | ok |
| 21 | what reminders do I have | list_reminders | You have two reminders. Call mum at six in the evening, and feed the c | ok |
| 22 | cancel the reminder about the cat | list_reminders, cancel_reminder | I've cancelled that reminder. | ok |
| 23 | do I have anything on tomorrow |  | I can't check your calendar, since I don't have access to it. | FAILED |
| 24 | what reminders do I have | list_reminders | You don't have any reminders right now. | ok |
| 26 | remember that my sister's name is Ana | save_memory | Okay, I'll remember that. | ok |
| 27 | I'm allergic to peanuts, keep that in mind | save_memory | Okay, I'll remember that. | ok |
| 28 | my favourite tea is mint | save_memory | Okay, I'll remember that. | ok |
| 29 | what's my sister called |  | Your sister is called Ana. | ok |
| 30 | what do you know about me |  | You like mint tea, and your sister is called Ana. | ok |
| 31 | forget that I like mint tea | search_memory | Do you want me to forget that you like mint tea? | ok |
| 32 | what's my dog's name | search_memory | rate_limited: The AI service is busy. Try again in a moment. | FAILED |
| 34 | turn the volume up | set_volume | Volume 70 percent. | ok |
| 35 | that's too loud |  | rate_limited: The AI service is busy. Try again in a moment. | FAILED |
| 36 | set the volume to thirty percent | set_volume | Volume 30 percent. | ok |
| 37 | speak a bit quieter please | set_volume | Volume 50 percent. | ok |
| 39 | thanks |  | You are welcome. | ok |
| 40 | never mind | (device) |  | ok |
| 41 | good morning |  | Good afternoon! How can I help you today? | ok |
| 43 | how many grams are in a pound |  | There are four hundred fifty-three point six grams in a pound. | ok |
| 44 | what's the capital of Slovenia |  | Ljubljana is the capital. | ok |
| 45 | what day is it tomorrow |  | Tomorrow is Saturday. | ok |
| 46 | explain how a rainbow forms |  | Sunlight shines through raindrops and bends. The light reflects inside | ok |
| 48 | turn on the living room lights |  | I cannot control other devices. | ok |
| 49 | what's the weather like tomorrow |  | I cannot check the weather. Would you like me to do something else? | ok |
| 50 | play some jazz |  | I cannot play music. Can you play some on your phone instead? | ok |
| 51 | send a message to Ana saying I'm late |  | I cannot send messages. Would you like me to set a reminder instead? | ok |
| 53 | what are you |  | I am Trstenko, a small desk robot with a face on my screen. | ok |
| 54 | could you time five minutes for me | set_timer | Timer set for five minutes. | ok |
| 56 | what's my name |  | Your name is Maj. | ok |
