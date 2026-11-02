"""The 80 fixed owner chat messages for the v1 end-to-end acceptance (spec §2).

Rules (checked by fixtures_v1.validate_pool): 60-120 English words each, the
owner talking about their own life, no cat / pet, nothing about how long her
replies should be, no questions, and none of the truth values of truth.json.
Frozen after the pilot: never reorder or edit once a scored session has run.
"""

CHAT_POOL: list[str] = [
    # 1
    "I finally fixed the squeaking hinge on the bathroom door this morning. It has been driving me up the wall for months, because every time I got up at night it sounded like a haunted house. I watched a video, pulled the pin out, scrubbed it with an old toothbrush and rubbed a bit of olive oil on it since I could not find any proper oil. It works for now. I felt weirdly proud of myself, like I had built the whole door from scratch.",
    # 2
    "My sister called during lunch to tell me she got the job at the architecture firm she interviewed with in September. She was so nervous about the second round that she rehearsed her portfolio talk on me over video three times. I could hear her grinning through the phone. We are going to celebrate at that dumpling place near her flat once she starts, and I already promised to pay, which my wallet may regret later.",
    # 3
    "The bus this morning was packed because of the rain, and I ended up standing next to a guy holding a huge potted fern. Every time the driver braked, the fern slapped me gently in the face. He kept apologizing and I kept saying it was fine, and by the third stop we were both laughing so hard that other people started smiling too. Strange way to start a Thursday, but honestly it put me in a good mood.",
    # 4
    "I tried making focaccia over the weekend for the first time. The recipe said to let the dough rise twice, and I got impatient and cut the second rise in half, so it came out dense on one side and fluffy on the other. Still, the rosemary and sea salt on top made the whole kitchen smell incredible. I ate a third of it standing at the counter before it even cooled down, which I am not proud of.",
    # 5
    "At work we had a long planning session about next year, and halfway through, the projector died. Nobody could find the right cable, so my manager started drawing the roadmap on the whiteboard with a marker that was almost out of ink. By the end, the board was covered in faint grey boxes and arrows nobody could read. We took a picture of it anyway. I suspect that picture will be the official plan forever now.",
    # 6
    "I have been going to the climbing gym on Tuesday evenings with a friend from my old job. Tonight I finally finished the purple route on the overhang wall that I had been failing at for three weeks. My forearms were shaking at the top and I almost let go right before the last hold. When I came down, a teenager I have never met gave me a high five. My hands still feel like jelly as I type this.",
    # 7
    "My upstairs neighbor started learning the cello. For the first week it sounded like someone dragging furniture across the floor, but tonight I recognized an actual melody, something slow and a little sad. I stood in the kitchen with the kettle in my hand and just listened until it ended. I might leave a note on her door saying it sounds good, though I worry she would be embarrassed that people can hear her practicing.",
    # 8
    "I cleaned out the hallway closet and found a box of letters my grandfather wrote to my grandmother when he was working at a shipyard far from home. His handwriting is tiny and slanted, and he signs every single one with a little drawing of a boat. I only read two before I had to stop because I was getting emotional. I think I will scan them so my cousins can read them too.",
    # 9
    "The coffee shop near the office changed owners, and the new people repainted everything a bright mint green. The coffee is better now, much less bitter, but they took away the old leather armchair by the window that I always sat in. I spent the whole morning feeling slightly betrayed by a piece of furniture. I know it is silly. I will probably get used to the new stools within a week anyway.",
    # 10
    "My knee has been bothering me since I started running again, so I finally went to a physiotherapist. She watched me jog on a treadmill for a few minutes and said my stride is too long and I land on my heel like I am trying to stomp out a fire. Now I have a set of boring exercises with a rubber band to do every evening. I already skipped them once, which is a bad sign.",
    # 11
    "I watched an old black and white detective movie last night, one of those where everyone talks very fast and wears a hat indoors. I guessed the killer in the first twenty minutes and felt extremely smart, but then the movie added a twist and it turned out I was completely wrong. The real culprit was the quiet piano teacher. I sat there in the dark feeling outsmarted by a film made before my parents were born.",
    # 12
    "Our team lead announced she is leaving at the end of the month to move back to her hometown and take care of her father. Everyone is sad, because she is the one person who actually protects us from pointless meetings. We are planning a small farewell lunch and someone suggested we each write one memory of working with her. I keep trying to pick one and I have too many good ones.",
    # 13
    "I bought a cheap secondhand bicycle from a student who was moving abroad. The brakes squeal, the bell is missing and the seat is cracked, but it rides surprisingly smoothly. I took it along the canal path this afternoon and realized I had not ridden a bike in almost ten years. The first five minutes I wobbled like a toddler. After that it all came back, and I felt like a kid again.",
    # 14
    "My dad sent me a long voice message about his tomato plants. Apparently a squirrel has been stealing exactly one tomato every morning, and he has become obsessed with catching it in the act. He has set up a folding chair by the window and drinks his coffee there at dawn. He sounded happier than I have heard him in months. Retirement is clearly treating him well, squirrel war and all.",
    # 15
    "I had to give a presentation to a client today and I was so nervous I barely slept. In the end it went fine. Their director asked a tough question about costs that I had not prepared for, but I said I would get back to them with numbers rather than making something up, and she seemed to respect that. On the train home I felt the adrenaline drain away and nearly fell asleep standing up.",
    # 16
    "There is a little bakery two streets over that only opens on weekend mornings, and the line is always around the corner. Today I finally got up early enough to beat the crowd. The owner is an older man who hums while he works and slips an extra cinnamon roll into the bag if you say good morning to him. I said good morning very enthusiastically. It worked, and I regret nothing.",
    # 17
    "My laptop fan has started making a grinding noise whenever I open too many browser tabs, which is always. A coworker said I should clean the dust out, so I watched a tutorial and took the bottom panel off. There was a grey felt carpet of dust inside. I cleaned it with a soft brush and now it is almost silent. I am a little disgusted with myself and also very satisfied.",
    # 18
    "I started a jigsaw puzzle on the dining table, a thousand pieces showing a market full of colorful umbrellas. It seemed relaxing in the shop. In reality, there are about four hundred pieces that are just slightly different shades of blue sky. I have spent two evenings sorting them by tiny differences in tone. My dining table is now unusable and I eat on the couch like a raccoon.",
    # 19
    "A friend from university is getting married next spring, and she asked me to give a speech at the reception. I am honored but also terrified, because public speaking at weddings feels like walking a tightrope in front of grandparents. I started writing a draft tonight and it is mostly embarrassing stories about the two of us getting lost on a hiking trip. I will need to cut most of them.",
    # 20
    "I got caught in the rain walking home and decided not to run for once. I just walked slowly and let myself get soaked. The streets were almost empty, the shop windows were glowing, and there was this nice smell of wet stone everywhere. By the time I reached the door my shoes were squelching and my hair was dripping, but I felt oddly calm, like the rain washed the whole day off me.",
    # 21
    "The office kitchen has a new rule that everyone must label their food in the fridge, after someone kept eating other people's yogurts. Today a note appeared on the fridge door written in angry capital letters, asking the yogurt thief to confess. Nobody confessed, of course. Now the whole floor is gossiping about who it might be. I have my suspicions about the intern who always looks hungry.",
    # 22
    "I tried a pottery class for the first time with a coworker. The teacher made centering the clay on the wheel look effortless, and when I tried it, the clay flew off and hit the wall with a wet slap. By the end of the evening I had made one lopsided bowl that looks more like an ashtray. I am weirdly attached to it. They will fire it next week and I get to glaze it.",
    # 23
    "My brother visited for the weekend with his two kids. We built a blanket fort in the living room that took over the entire floor, and the youngest insisted on sleeping in it. At three in the morning I heard a small voice asking for water, then a crash as the fort collapsed on top of him. He was totally fine and thought it was hilarious. I found pillows in strange places all Sunday.",
    # 24
    "I finally finished reading that huge fantasy novel I started in the summer. The last two hundred pages I read in one sitting, staying up way too late. The ending was bittersweet, the main characters do win, but at a real cost, and one of my favorites does not make it. I closed the book and just sat there for a while. It is strange to miss people who never existed.",
    # 25
    "The heating in my apartment is broken again, so I spent the evening wrapped in two blankets with a hot water bottle on my lap. The landlord says a repair person will come on Monday. Until then, I am cooking soup every night mostly for the warmth of the stove. Tonight it was lentil soup with too much cumin. It was still good, and the kitchen is the warmest room in the flat.",
    # 26
    "I went to a friend's band concert in a tiny basement bar. There were maybe thirty people there, and half of them were friends of the band. The sound was terrible, the drummer lost a drumstick in the middle of a song and kept going with one hand, and everyone cheered. It was the most fun I have had at a concert in years. Small, messy shows have a magic the big ones lack.",
    # 27
    "I decided to try waking up earlier to go for a walk before work. The first morning I snoozed the alarm four times. The second morning I actually went out, and the city looked completely different at dawn, quiet streets, a street sweeper humming along, birds being very loud about everything. I bought a warm pastry from a bakery just opening its shutters. I am not a morning person, but I could become one.",
    # 28
    "Our department got new chairs today, the expensive ergonomic kind with a hundred levers. I spent twenty minutes adjusting mine and somehow made it worse, so it now tilts backwards whenever I relax. A coworker walked by and showed me the one lever I had missed. Everyone else seems to love theirs. I keep thinking fondly of the old squeaky chair that just did what it was told.",
    # 29
    "My best friend from high school messaged me out of nowhere after almost three years. She found an old video of us dancing badly at a school festival and sent it with a string of laughing emojis. We ended up talking for two hours about everything that happened since. It felt like no time had passed at all. We promised not to let it slip that long again, and I mean it this time.",
    # 30
    "I made a spreadsheet to track my spending this month, and the results are humbling. I spend an embarrassing amount on takeaway coffee and late-night snacks from the convenience store by the station. I told myself I would bring a thermos to work, and today I actually did. The coffee tasted worse than the shop's, but I felt very virtuous drinking it at my desk.",
    # 31
    "There was a small street festival in my neighborhood today, with food stalls, a brass band and kids running around with painted faces. I bought grilled corn and a skewer of something spicy that made my eyes water. An older couple was dancing in front of the band like nobody was watching. I sat on a bench for an hour just people-watching. Days like this make me like where I live a lot more.",
    # 32
    "I accidentally shrank my favorite wool sweater in the wash. It went from a cozy oversized fit to something that would fit a large doll. I was annoyed for about ten minutes and then laughed, because I had been ignoring the care label for years and it finally caught up with me. My niece is going to get a very nice, slightly fuzzy sweater for her birthday.",
    # 33
    "We had a fire drill at the office this afternoon, and the whole building stood in the parking lot for forty minutes in the cold wind. Someone from accounting brought out a tray of cookies they had baked for a birthday, and suddenly it turned into a little outdoor party. By the time we were allowed back inside, people from different floors were chatting like old friends. Best fire drill ever.",
    # 34
    "I started learning to play the guitar again after giving up in my twenties. My fingertips hurt so much from pressing the strings, and my chord changes are slow and clumsy. But tonight I managed to play the first verse of an old folk song all the way through without stopping. I recorded it on my phone and listened back. It sounded rough, but it was recognizably a song.",
    # 35
    "My grandmother taught me her recipe for stuffed peppers over the phone today. She does not measure anything, so every instruction was something like a handful of rice or enough cheese until it looks right. I took notes as best I could and tried it tonight. They were good, but not like hers. I think the secret ingredient is probably fifty years of practice and a bit of stubbornness.",
    # 36
    "I spent the afternoon repotting all my plants, which have been sulking in pots that are too small. The monstera was so root-bound that I had to cut the pot open with scissors. There is soil everywhere, on the balcony, the floor, somehow inside my sock. The plants already look happier though, or maybe I am imagining it. Either way, I am going to sleep very well tonight.",
    # 37
    "A colleague from the design team showed me a trick in the drawing software that saves me about an hour every week. I had been doing the same thing manually for two years, clicking through menus like a fool. She showed me in about thirty seconds. I bought her a fancy hot chocolate as thanks. It is humbling how much time we waste just because nobody tells us there is a better way.",
    # 38
    "I went to the dentist today for the first time in too long. I was expecting a lecture, and I got one, along with a cleaning that felt like someone sandblasting my teeth. The good news is no cavities. The dentist told me to floss every day, and I nodded seriously, knowing deep down that I will floss enthusiastically for one week and then forget.",
    # 39
    "The library near my office has a used book sale this week, everything for one coin. I went in to look for ten minutes and came out an hour later with a bag of seven books, including a cookbook from the seventies with alarming jelly recipes and a travel guide to a country that changed its name since. The librarian seemed delighted that someone took the jelly book.",
    # 40
    "My manager asked me to mentor a new hire who started this week. He is very sharp but extremely quiet, and he apologizes before every question as if asking is a crime. Today I told him that the questions were the best part of his work so far, because they showed he was actually thinking. He smiled for the first time. I remember being exactly that nervous at my first job.",
    # 41
    "I woke up to snow this morning, the first of the season, earlier than anyone expected. The whole street was white and silent, and the kids from the building next door were already outside trying to build a snowman out of what was barely an inch of snow. Their snowman was mostly mud and leaves, but they were very proud. I took the long way to work just to enjoy it.",
    # 42
    "I signed up for a beginner's evening course in conversational Spanish. There are eight of us, ranging from a retired engineer to a college student. Tonight we had to introduce ourselves and describe our favorite food, and I panicked and said I love potatoes very much, in a weirdly formal way. The teacher was kind about it. I am already looking forward to next week.",
    # 43
    "I helped my friend move into her new apartment on the fourth floor of a building with no elevator. We carried a sofa up those stairs and got it stuck on the landing for a solid ten minutes, twisting it every way possible. Eventually we had to take the legs off. My arms are destroyed. She paid us in pizza and homemade lemonade, which almost made up for it.",
    # 44
    "The power went out in my whole neighborhood tonight for about two hours. At first I was annoyed, because I was halfway through cooking, but then I lit some candles and finished my noodles on the gas stove by candlelight. Without my phone and laptop, I ended up reading by flashlight and listening to the neighbors talking on their balconies. It felt like an old village evening.",
    # 45
    "I finally organized my desk drawers at work. I found three staplers, a broken umbrella, receipts from two years ago, a birthday card I forgot to give someone, and an unopened pack of gum that had turned into a single solid brick. I threw away almost everything. My drawers look so empty now that I feel like a new employee. I give it a month before the chaos returns.",
    # 46
    "I spent the evening at my aunt's place helping her set up a new phone. She wanted to send photographs to the family group chat, but every time she tried, she accidentally started a video call with someone. We called my uncle three times by mistake. He eventually answered and asked if everything was all right. We laughed so much that she gave up and made us tea instead.",
    # 47
    "I went to an art exhibition with huge abstract paintings, mostly splashes of red and black on enormous canvases. I usually do not understand abstract art, but one painting made me stand there for ten minutes without knowing why. It looked like a storm seen from inside a car. I bought a postcard of it at the gift shop and stuck it on my fridge when I got home.",
    # 48
    "The vending machine at work ate my coins again today and gave me nothing. This is the third time this month. I have started a small campaign with my coworkers where we put a sticky note on it every time it steals from someone, and now it is covered in little yellow notes like a wanted poster. Facilities finally sent someone to fix it, and he laughed at the notes.",
    # 49
    "I went swimming at the public pool for the first time in years. I forgot how hard swimming laps is. After four lengths I was clinging to the edge gasping, while an elderly lady in a flowered cap glided past me without effort for the whole hour. Afterwards I sat in the warm sauna feeling defeated and inspired at the same time. I am going to go back on Saturday.",
    # 50
    "I watched the sunset from the hill behind the train station after work. There is a bench up there that almost nobody uses, and the whole city spreads out below, with windows lighting up one by one. I ate a sandwich and watched the sky change from orange to pink to deep blue. I do not do that often enough. I should make it a habit at least once a week.",
    # 51
    "Our building's elevator broke and the repair team cannot come until next week, so everyone is taking the stairs. I live on the sixth floor, which is not a lot, but carrying groceries up is a workout. I have started meeting neighbors on the landings, all of us panting and laughing about it. I learned more names this week than in the whole year I have lived here.",
    # 52
    "I baked banana bread with the overripe bananas that had been staring at me from the counter for a week. I added walnuts and a bit of dark chocolate, and it came out perfect for once, moist, not burnt on the bottom. I brought half of it to work and it was gone within an hour. Two people asked for the recipe, and I felt like a celebrity chef.",
    # 53
    "I had a strange dream last night that I was back in school taking a math exam, but all the questions were about the history of sandwiches. I woke up in a panic, convinced I had failed. It took me a full minute to remember that I graduated a long time ago. I told my coworkers about it at lunch and now everyone is sharing their weirdest exam dreams.",
    # 54
    "I joined a neighborhood clean-up this morning along the river. About twenty of us picked up trash for three hours, and we found the oddest things: a shopping cart, a single high-heeled shoe, a toy dinosaur and an old rotary telephone. A local cafe gave everyone free coffee afterwards. My back hurts, but the riverbank looks so much better, and I met some lovely people.",
    # 55
    "I finally called the internet company to complain about my connection dropping every evening. I was on hold for forty minutes listening to the same three seconds of violin music. When I finally reached someone, she was so nice and so tired that I could not stay angry. She fixed something on her end in two minutes. I hope she gets a long holiday soon.",
    # 56
    "My niece had her school play this afternoon, and she played a tree. She had exactly one line, which was to say that the wind is coming, and she delivered it with the intensity of a great dramatic actress. The whole family clapped like it was a sold-out theater. She came out afterwards still wearing her cardboard leaves and asked if she was the best tree. She absolutely was.",
    # 57
    "I tried meditating before bed using an app a friend recommended. The voice on the recording was very calm and asked me to notice my breathing and let my thoughts float by like clouds. My thoughts did not float by. They marched past in a loud parade, mostly about emails I forgot to send. Still, I slept better than usual, so maybe something worked after all.",
    # 58
    "My coworker brought her baby to the office for a quick visit today, and productivity on the whole floor dropped to zero. People who never talk to each other were suddenly cooing together around the tiny visitor. The baby stared at all of us with great seriousness and then fell asleep on her mother's shoulder. Somehow the whole afternoon felt softer after that.",
    # 59
    "I went to a farmers market and bought far too many vegetables because everything looked so fresh. Now my fridge is overflowing with leeks, beets, kale and a pumpkin the size of a small boulder. I spent the evening making a giant pot of vegetable stew to use some of it up. I will be eating stew for days, and I am not complaining.",
    # 60
    "Today I found out that the old record shop downtown is closing at the end of the year. I used to spend whole Saturdays there as a teenager, flipping through dusty boxes of vinyl and pretending I knew about jazz. I went in after work and bought one last record, a soul album with a scratched cover. The owner remembered my face, which surprised me a lot.",
    # 61
    "I made a terrible mistake today and sent an email meant for my team to the entire company. It was nothing scandalous, just a message saying the coffee machine on our floor tastes like burnt rubber, but it went to hundreds of people. Several strangers replied agreeing with me. Facilities ordered a new machine by the afternoon. I am choosing to see it as a success.",
    # 62
    "My old university professor gave a public lecture tonight about the history of the city's bridges, and I went on a whim. He still makes the same jokes, still waves his hands wildly when excited. Afterwards I said hello and was shocked that he remembered my final essay. He told me it was too long but full of good ideas, which is exactly what he told me back then.",
    # 63
    "I went to see a movie alone this evening for the first time. I always thought it would feel sad or awkward, but it was great. I chose the seat I wanted, bought the giant popcorn without sharing and did not have to discuss the plot afterwards. It was a quiet science fiction film about a lighthouse keeper on another planet, and I cried a little at the end.",
    # 64
    "I spent the afternoon at my friend's garden helping her harvest apples from the old tree behind her house. We filled four baskets, and she sent me home with a whole bag. Tonight I tried to make an apple crumble from memory. I forgot the sugar in the topping, so it tasted healthy and sad, but with a scoop of ice cream on top it was rescued completely.",
    # 65
    "There is a busker at my train station who plays the accordion every morning. I have walked past him for months without stopping. Today he was playing a waltz my grandmother used to hum while cooking, and I just stood there until he finished. I put some money in his case and told him why. He smiled and played it again for me, slower this time.",
    # 66
    "I tried to assemble a bookshelf from a flat-pack kit tonight. The instructions were only pictures, and one picture clearly showed a person smiling while holding a piece that does not exist in my box. After two hours, I have a bookshelf that is slightly leaning, three leftover screws and a feeling that the furniture is laughing at me. It holds books though.",
    # 67
    "My company had its annual volunteer day and our team spent it painting a community center for elderly residents. Some of the residents sat nearby and supervised us very strictly, pointing out every drip and missed spot. One gentleman told us long stories about the neighborhood in the old days. I learned more local history in one afternoon than in years of living here.",
    # 68
    "I finally deleted all the old apps on my phone that I never use. There were three different weather apps, a language game I abandoned, a meditation timer and an app for a restaurant that closed two years ago. My phone feels lighter somehow, like a cleaned room. I know it does not matter at all, but it gave me a small sense of control over my life.",
    # 69
    "Today was my coworker's last day before parental leave, and we decorated his desk with paper storks and tiny baby socks. He pretended to be embarrassed but he was clearly touched. He gave a little speech about how he will miss our terrible office coffee. We all promised to keep his plant alive while he is gone, which, given our track record, is a bold promise.",
    # 70
    "I visited the natural history museum on my day off. The dinosaur hall was full of school groups, so I wandered into a quiet room full of minerals and crystals instead. There was a huge amethyst geode taller than me, glowing purple under the lights. I stood there for a long time thinking about how long it took to grow. It made my problems feel very small.",
    # 71
    "I got a parking ticket today because I misread a sign that seems designed by someone who hates drivers. It had four different time ranges and an exception for holidays that I still do not understand. I was annoyed, but then the officer who wrote the ticket admitted the sign confuses everyone. He suggested I write to the city about it. I might actually do that.",
    # 72
    "My friend and I tried a new ramen place that just opened near the river. The broth was so rich it was almost creamy, and the noodles were perfectly chewy. The chef came out to ask how everything was, and when we praised the broth, he told us he simmers it for eighteen hours. We are already planning to go back. I may have found my new favorite restaurant.",
    # 73
    "I spent the evening going through old photographs on my laptop from when I was a kid, scanning in pictures my mother sent me in an envelope. There is one of me at about six years old, wearing a superhero cape over pajamas and a very determined face. I sent it to my brother and he immediately made it his profile picture. I will get my revenge eventually.",
    # 74
    "Work was quiet today because half the team is at a conference, so I finally had time to clean up the old documentation nobody has touched in years. Some of the pages were written by people who left before I joined, full of inside jokes I do not understand. It felt like digging through a time capsule. I kept one of the jokes in, as a tribute.",
    # 75
    "I tried a new hiking trail on Sunday that a coworker recommended. The map said it was an easy walk, but the map lied. It was mostly steep, rocky climbing through a forest, with roots everywhere waiting to trip me. At the top though, there was a view over the whole valley, and the clouds were rolling below us. I ate my sandwich up there feeling like a mountaineer.",
    # 76
    "My upstairs neighbor knocked on my door today with a plate of homemade cookies, as a thank you for keeping her packages while she was away. We ended up talking in the doorway for half an hour about gardening and her grandchildren. I realized I have lived below her for two years and never had a real conversation with her. I am going to bake something to bring back.",
    # 77
    "I finally fixed the leaky kitchen tap that has been dripping for weeks. It turned out to be a worn-out rubber washer that cost almost nothing at the hardware store. The man at the shop drew me a little diagram on the back of a receipt showing what to do. It took me twenty minutes and only one minor flood. The silence at night is wonderful now.",
    # 78
    "Today at lunch, a group of us started talking about the first concerts we ever went to. Mine was a terrible pop band that I was obsessed with at thirteen, and I wore a homemade t-shirt with their faces painted on it. One coworker went to an opera with her grandfather, another went to a heavy metal festival at age ten. We laughed through the whole lunch break.",
    # 79
    "I went for a long walk along the coast this weekend. The wind was so strong that I had to lean into it to keep moving, and my hair turned into a bird's nest. The waves were crashing up over the sea wall, and a few brave surfers were out there anyway. I bought hot chips from a kiosk and ate them in the car with the heater on, perfectly content.",
    # 80
    "My best friend and I have a tradition of cooking a ridiculous meal together once a month, something neither of us has ever made. Tonight it was homemade dumplings from scratch. The dough was too thick, half of them burst in the pot, and the kitchen looked like a flour bomb exploded. But the ones that survived were delicious, and we laughed the whole evening.",
]
