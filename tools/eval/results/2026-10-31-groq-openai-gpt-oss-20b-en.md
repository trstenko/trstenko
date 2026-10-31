# Evaluation: groq, openai/gpt-oss-20b

Run 2026-10-31.

**42 of 44 cases passed.**

| Check | Passed |
|---|---|
| arguments | 17 of 18 |
| asks back | 4 of 4 |
| called once | 24 of 24 |
| does not say | 5 of 6 |
| length | 44 of 44 |
| local | 5 of 5 |
| no extra tool | 24 of 24 |
| says | 8 of 8 |
| tool | 44 of 44 |
| turn | 44 of 44 |

Time to the first sentence: median 0.4s, slowest 1.1s.
Requests: 47. Cost by the device's own meter: $0.0046.

## What failed

- **"remind me on Monday at nine to email the teacher"** (line 7)
  - arguments: when='2026-10-09T09:00', wanted '2026-10-05T09:00'
- **"what are you"** (line 53)
  - does not say: said: I’m a little desk robot with a face on a screen, and I talk out loud. Meow.

## Every case

| Line | Said | Tools | Reply | Result |
|---|---|---|---|---|
| 4 | remind me to call mum at six | set_reminder | I'll remind you at six in the evening. | ok |
| 5 | remind me to take the bins out tomorrow at seven in the morning | set_reminder | I'll remind you tomorrow at seven in the morning. | ok |
| 6 | remind me at half past eight tonight to charge the bike lights | set_reminder | I'll remind you at half past eight in the evening. | ok |
| 7 | remind me on Monday at nine to email the teacher | set_reminder | I'll remind you on 9 October at nine in the morning. | FAILED |
| 8 | remind me to water the plants in two hours | set_reminder | I'll remind you at five ten in the afternoon. | ok |
| 9 | remind me to stretch in twenty minutes | set_reminder | I'll remind you at half past three in the afternoon. | ok |
| 10 | at noon tomorrow remind me about the dentist | set_reminder | I'll remind you tomorrow at noon. | ok |
| 11 | remind me to buy milk at three | set_reminder | It looks like you meant a time that’s already passed. Do you want to b | ok |
| 12 | remind me to call grandma |  | Sure, when would you like to be reminded? | ok |
| 14 | set a timer for ten minutes | set_timer | Timer set for ten minutes. | ok |
| 15 | timer for ninety seconds | set_timer | Timer set for one minute and thirty seconds. | ok |
| 16 | start a pasta timer for twelve minutes | set_timer | Timer set for twelve minutes. | ok |
| 17 | wake me up in an hour and a half | set_reminder | I'll remind you at four forty in the afternoon. | ok |
| 18 | can you count down five minutes for my tea | set_timer | Timer set for five minutes. | ok |
| 19 | set a timer |  | Kakšen čas? | ok |
| 21 | what reminders do I have | list_reminders | You have two reminders: one to call mum at six in the evening, and one | ok |
| 22 | cancel the reminder about the cat | list_reminders, cancel_reminder | All right, I've cancelled the reminder to feed the cat. | ok |
| 23 | do I have anything on tomorrow | list_reminders | You have a reminder to feed the cat tomorrow at eight in the morning. | ok |
| 24 | what reminders do I have | list_reminders | You don’t have any active reminders right now. | ok |
| 26 | remember that my sister's name is Ana | save_memory | Okay, I'll remember that. | ok |
| 27 | I'm allergic to peanuts, keep that in mind | save_memory | Okay, I'll remember that. | ok |
| 28 | my favourite tea is mint | save_memory | Okay, I'll remember that. | ok |
| 29 | what's my sister called |  | Your sister’s name is Ana. | ok |
| 30 | what do you know about me | search_memory | I know you like mint tea and your sister is called Ana. | ok |
| 31 | forget that I like mint tea |  | Sure, do you want me to forget that? | ok |
| 32 | what's my dog's name |  | I’m not sure. Could you tell me your dog’s name? | ok |
| 34 | turn the volume up | set_volume | Volume 70 percent. | ok |
| 35 | that's too loud | set_volume | Volume 50 percent. | ok |
| 36 | set the volume to thirty percent | set_volume | Volume 30 percent. | ok |
| 37 | speak a bit quieter please | set_volume | Volume 50 percent. | ok |
| 39 | thanks |  | You're welcome. | ok |
| 40 | never mind | (device) |  | ok |
| 41 | good morning |  | Good morning! How can I help you today? | ok |
| 43 | how many grams are in a pound |  | About 454 grams in a pound. | ok |
| 44 | what's the capital of Slovenia |  | Ljubljana. | ok |
| 45 | what day is it tomorrow |  | Tomorrow is Saturday, the third of October. | ok |
| 46 | explain how a rainbow forms |  | A rainbow forms when sunlight hits raindrops and is refracted, reflect | ok |
| 48 | turn on the living room lights |  | I’m sorry, but I can’t control the lights. | ok |
| 49 | what's the weather like tomorrow |  | I’m sorry, but I don’t have weather data. | ok |
| 50 | play some jazz |  | I’m not able to play music. Would you like me to find a jazz playlist  | ok |
| 51 | send a message to Ana saying I'm late |  | I’m sorry, but I can’t send messages for you. | ok |
| 53 | what are you |  | I’m a little desk robot with a face on a screen, and I talk out loud.  | FAILED |
| 54 | could you time five minutes for me | set_timer | Timer set for five minutes. | ok |
| 56 | what's my name |  | Maj. | ok |
