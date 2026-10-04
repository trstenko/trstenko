# Evaluation: gemini, gemini-3.5-flash-lite

Run 2026-10-04.

**38 of 40 cases passed.**

| Check | Passed |
|---|---|
| arguments | 18 of 19 |
| asks back | 3 of 3 |
| called once | 22 of 24 |
| does not say | 6 of 6 |
| length | 40 of 40 |
| local | 2 of 2 |
| no extra tool | 24 of 24 |
| says | 8 of 8 |
| tool | 40 of 40 |
| turn | 40 of 40 |

Time to the first sentence: median 0.7s, slowest 1.8s.
Requests: 48. Cost by the device's own meter: $0.0215.

## What failed

- **"zapomni si, da je moji sestri ime Ana"** (line 26)
  - called once: save_memory called 2 times
- **"moj najljubši čaj je metin"** (line 29)
  - called once: save_memory called 2 times
  - arguments: text="The user's favorite tea is mint tea." lacks 'met'

## Every case

| Line | Said | Tools | Reply | Result |
|---|---|---|---|---|
| 5 | opomni me ob šestih, naj pokličem mamo | set_reminder | Opomnik je nastavljen za danes ob šestih zvečer. | ok |
| 6 | jutri ob sedmih zjutraj me opomni, naj odnesem smeti | set_reminder | Opomnik je nastavljen za jutri ob sedmih zjutraj. | ok |
| 7 | nocoj ob pol devetih me opomni, naj napolnim luči za kolo | set_reminder | To že velja. Opomnik je nastavljen za danes ob pol devetih zvečer. | ok |
| 8 | v ponedeljek ob devetih me opomni, naj pišem učiteljici | set_reminder | Opomnik je nastavljen za ponedeljek ob devetih zjutraj. | ok |
| 9 | čez dve uri me opomni, naj zalijem rože | set_reminder | Opomnik je nastavljen za danes ob petih in deset minut popoldne. | ok |
| 10 | opomni me čez dvajset minut, naj se pretegnem | set_reminder | Seveda. Opomnik je nastavljen za danes ob pol štirih popoldne. | ok |
| 11 | jutri opoldne me opomni na zobozdravnika | set_reminder | Opomnik je nastavljen za jutri opoldne. | ok |
| 12 | opomni me ob četrt na osem zvečer, da je tekma | set_reminder | Opomnik je nastavljen za danes ob sedmih in petnajst minut zvečer. | ok |
| 13 | opomni me, naj pokličem babico |  | Kdaj pa želite, da vas opomnim? | ok |
| 15 | nastavi časovnik na deset minut | set_timer | Časovnik je nastavljen za deset minut. | ok |
| 16 | časovnik za devetdeset sekund | set_timer | Časovnik je nastavljen za eno minuto in trideset sekund. | ok |
| 17 | nastavi časovnik za testenine na dvanajst minut | set_timer | Časovnik je nastavljen za dvanajst minut. | ok |
| 18 | odštej pet minut za moj čaj | set_timer | Časovnik je nastavljen za pet minut. | ok |
| 19 | zbudi me čez uro in pol | set_reminder | Opomnik je nastavljen za danes ob štirih in štirideset minut popoldne. | ok |
| 20 | nastavi časovnik |  | Za koliko časa? | ok |
| 22 | katere opomnike imam | list_reminders | Dva opomnika imaš. Ob šestih zvečer moraš poklicati mamo, jutri ob osm | ok |
| 23 | prekliči opomnik za mačko | list_reminders, cancel_reminder | Opomnik je preklican. | ok |
| 24 | ali imam jutri kaj v načrtu | list_reminders | Jutri ob osmih zjutraj imaš opomnik za nahrani mačko. | ok |
| 26 | zapomni si, da je moji sestri ime Ana | save_memory, save_memory | V redu, shranjeno. | FAILED |
| 27 | zapomni si, da imam rad metin čaj | save_memory | V redu, shranjeno. | ok |
| 28 | alergičen sem na arašide, zapomni si to | save_memory | V redu, shranjeno. | ok |
| 29 | moj najljubši čaj je metin | save_memory, save_memory | V redu, shranjeno. | FAILED |
| 30 | kako je ime moji sestri |  | Vaši sestri je ime Ana. | ok |
| 31 | pozabi, da imam rad metin čaj | search_memory | Ali res želiš, da pozabim, da imaš rad metin čaj? | ok |
| 32 | kako je ime mojemu psu |  | Ničesar nimam zapisanega o tvojem psu. Kako mu je ime? | ok |
| 34 | povečaj glasnost | set_volume | Glasnost je nastavljena na 70 odstotkov. | ok |
| 35 | to je preglasno | set_volume | Glasnost je nastavljena na 50 odstotkov. | ok |
| 36 | nastavi glasnost na trideset odstotkov | set_volume | Glasnost je nastavljena na 30 odstotkov. | ok |
| 37 | govori malo tišje, prosim | set_volume | Glasnost je nastavljena na 50 odstotkov. | ok |
| 39 | hvala |  | Ni za kaj. | ok |
| 40 | dobro jutro |  | Dober dan. Kako ti lahko pomagam? | ok |
| 42 | katero je glavno mesto Slovenije |  | Ljubljana je glavno mesto Slovenije. | ok |
| 43 | kateri dan je jutri |  | Jutri je sobota. | ok |
| 44 | koliko gramov je v kilogramu |  | V enem kilogramu je tisoč gramov. | ok |
| 45 | razloži, kako nastane mavrica |  | Mavrica nastane, ko sončni žarki posijejo skozi dežne kaplje. Svetloba | ok |
| 47 | prižgi luči v dnevni sobi |  | Tega ne znam narediti, ker nimam dostopa do tvojih luči. | ok |
| 48 | kakšno bo jutri vreme |  | Ne znam pogledati vremenske napovedi. Poglej na telefon ali računalnik | ok |
| 49 | predvajaj malo jazza |  | Tega žal ne znam. Lahko pa ti zavrtim kakšno drugo glasbo ali ti povem | ok |
| 51 | kaj si |  | Jaz sem majhen namizni robot. | ok |
| 53 | kako mi je ime |  | Ime ti je Maj. | ok |
