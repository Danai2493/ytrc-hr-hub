"""YTRC Growth Compass: the company's own competency framework (draft v1,
2026-10-30). Written for a Thai block rubber factory: original clusters,
definitions and behaviour questions. Broad competency areas are common in HR
practice; nothing here is copied from a commercial assessment.

Answers use how OFTEN the person shows the behaviour (1 rarely ... 5 almost
always), which is easier to answer honestly than "how good am I".
Audience: core = everyone (short, simple wording), staff = staff and above,
leader = people who lead a team. A question marked reverse scores 6 - answer.

The results help people see strengths and choose what to develop. They are
not a rating and do not feed pay or appraisal."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Competency, CompetencyQuestion, GrowthPath

CLUSTERS = [
    ("THINK", "Think and solve", "คิดและแก้ปัญหา"),
    ("PEOPLE", "Work with people", "ทำงานร่วมกับผู้อื่น"),
    ("SELF", "Manage myself", "บริหารตนเอง"),
    ("DELIVER", "Deliver results", "ส่งมอบผลงาน"),
    ("LEAD", "Lead and develop others", "นำและพัฒนาผู้อื่น"),
]

# code, cluster, English name, Thai name, English definition, Thai definition, leaders only
COMPETENCIES = [
    ("TH1", "THINK", "Problem solving", "การแก้ปัญหา",
     "Finds the real cause of a problem and fixes it so it does not come back.",
     "หาสาเหตุที่แท้จริงของปัญหาและแก้ไขไม่ให้เกิดซ้ำ", False),
    ("TH2", "THINK", "Planning and organising", "การวางแผนและจัดระบบงาน",
     "Plans work, time and materials so the job is ready when it is needed.",
     "วางแผนงาน เวลา และวัตถุดิบ ให้งานพร้อมเมื่อถึงเวลาที่ต้องใช้", False),
    ("TH3", "THINK", "Learning quickly", "การเรียนรู้อย่างรวดเร็ว",
     "Picks up new tasks, machines and rules quickly and uses what was learned.",
     "เรียนรู้งาน เครื่องจักร และกฎใหม่ได้เร็ว และนำไปใช้จริง", False),
    ("TH4", "THINK", "Improvement mindset", "การคิดปรับปรุงงาน",
     "Looks for small and big ways to make the work safer, easier, faster or cheaper.",
     "มองหาวิธีปรับปรุงงานให้ปลอดภัยขึ้น ง่ายขึ้น เร็วขึ้น หรือประหยัดขึ้น", False),
    ("PE1", "PEOPLE", "Clear communication", "การสื่อสารที่ชัดเจน",
     "Says and writes things so others understand, and listens to understand them.",
     "พูดและเขียนให้ผู้อื่นเข้าใจ และฟังเพื่อเข้าใจผู้อื่น", False),
    ("PE2", "PEOPLE", "Teamwork", "การทำงานเป็นทีม",
     "Helps colleagues, shares the load and puts the team result first.",
     "ช่วยเหลือเพื่อนร่วมงาน แบ่งเบาภาระ และให้ความสำคัญกับผลงานของทีม", False),
    ("PE3", "PEOPLE", "Working across teams", "การประสานงานข้ามหน่วยงาน",
     "Builds trust with other departments, suppliers and visitors to get things done together.",
     "สร้างความไว้วางใจกับหน่วยงานอื่น ผู้ขาย และผู้มาติดต่อ เพื่อให้งานสำเร็จร่วมกัน", False),
    ("PE4", "PEOPLE", "Next-process and customer focus", "ใส่ใจกระบวนการถัดไปและลูกค้า",
     "Thinks about the person who receives the work next, inside or outside the company.",
     "คำนึงถึงผู้รับงานต่อไป ทั้งภายในและภายนอกบริษัท", False),
    ("SE1", "SELF", "Responsibility and discipline", "ความรับผิดชอบและวินัย",
     "Keeps promises, follows the rules and the shift plan, and owns mistakes.",
     "รักษาคำพูด ปฏิบัติตามกฎและแผนกะ และยอมรับความผิดพลาดของตนเอง", False),
    ("SE2", "SELF", "Calm under pressure", "ความมั่นคงภายใต้แรงกดดัน",
     "Stays calm and keeps working well when things go wrong or time is short.",
     "ใจเย็นและทำงานได้ดีเมื่อเกิดปัญหาหรือเวลาเร่งด่วน", False),
    ("SE3", "SELF", "Honesty and integrity", "ความซื่อสัตย์",
     "Is honest in records, money, time and reports, even when no one checks.",
     "ซื่อสัตย์ในบันทึก เงิน เวลา และรายงาน แม้ไม่มีใครตรวจ", False),
    ("SE4", "SELF", "Growing myself", "การพัฒนาตนเอง",
     "Asks for feedback, sets own goals and takes chances to learn.",
     "ขอคำแนะนำ ตั้งเป้าหมายของตนเอง และใช้โอกาสในการเรียนรู้", False),
    ("DE1", "DELIVER", "Quality first", "คุณภาพมาก่อน",
     "Checks own work against the standard and does not pass a defect on.",
     "ตรวจงานของตนเองตามมาตรฐาน และไม่ส่งของเสียต่อ", False),
    ("DE2", "DELIVER", "Safety ownership", "ความปลอดภัยเป็นหน้าที่ของทุกคน",
     "Works safely, stops unsafe work and looks after the safety of others.",
     "ทำงานอย่างปลอดภัย หยุดงานที่ไม่ปลอดภัย และดูแลความปลอดภัยของผู้อื่น", False),
    ("DE3", "DELIVER", "Drive for results", "มุ่งมั่นสู่ผลสำเร็จ",
     "Keeps going until the target is met and finds a way around obstacles.",
     "ทำงานจนบรรลุเป้าหมาย และหาทางผ่านอุปสรรค", False),
    ("DE4", "DELIVER", "Cost and resource care", "ใส่ใจต้นทุนและทรัพยากร",
     "Uses materials, energy, water and time carefully, as if it were own money.",
     "ใช้วัตถุดิบ พลังงาน น้ำ และเวลาอย่างคุ้มค่า เหมือนเป็นเงินของตนเอง", False),
    ("LE1", "LEAD", "Coaching others", "การสอนงานและโค้ช",
     "Teaches skills step by step, checks understanding and gives useful feedback.",
     "สอนงานทีละขั้น ตรวจความเข้าใจ และให้คำแนะนำที่เป็นประโยชน์", True),
    ("LE2", "LEAD", "Leading a team", "การนำทีม",
     "Sets clear goals, shares work fairly and keeps the team motivated and safe.",
     "กำหนดเป้าหมายชัดเจน แบ่งงานอย่างเป็นธรรม และดูแลให้ทีมมีแรงจูงใจและปลอดภัย", True),
    ("LE3", "LEAD", "Sound decisions", "การตัดสินใจที่ดี",
     "Makes timely decisions with the facts available and explains the reasons.",
     "ตัดสินใจได้ทันเวลาจากข้อมูลที่มี และอธิบายเหตุผลได้", True),
    ("LE4", "LEAD", "Leading change", "การนำการเปลี่ยนแปลง",
     "Explains why a change is needed, brings people along and follows it through.",
     "อธิบายเหตุผลของการเปลี่ยนแปลง ชวนคนให้ร่วมมือ และติดตามจนสำเร็จ", True),
]

# competency, audience, English, Thai, reverse
QUESTIONS = [
    ("TH1", "core", "When something goes wrong, I look for why it happened, not only how to fix it today.", "เมื่อเกิดปัญหา ฉันหาสาเหตุว่าทำไมจึงเกิด ไม่ใช่แค่แก้ให้ผ่านไปวันนี้", False),
    ("TH1", "core", "I fix the same problem again and again without finding its cause.", "ฉันแก้ปัญหาเดิมซ้ำแล้วซ้ำอีก โดยไม่ได้หาสาเหตุ", True),
    ("TH1", "staff", "I use facts and data (records, samples, numbers) before deciding what caused a problem.", "ฉันใช้ข้อเท็จจริงและข้อมูล (บันทึก ตัวอย่าง ตัวเลข) ก่อนสรุปสาเหตุของปัญหา", False),
    ("TH2", "core", "Before my shift starts, I know what I must do and I prepare tools and materials.", "ก่อนเริ่มกะ ฉันรู้ว่าต้องทำอะไร และเตรียมเครื่องมือและวัตถุดิบไว้", False),
    ("TH2", "core", "I finish my work on time without others having to remind me.", "ฉันทำงานเสร็จตามเวลาโดยไม่ต้องให้ใครเตือน", False),
    ("TH2", "staff", "I plan my week, set priorities and warn others early when a deadline is at risk.", "ฉันวางแผนงานรายสัปดาห์ จัดลำดับความสำคัญ และแจ้งผู้อื่นล่วงหน้าเมื่องานอาจไม่ทันกำหนด", False),
    ("TH3", "core", "When I am shown a new task, I can do it myself after a short time.", "เมื่อได้รับการสอนงานใหม่ ฉันทำเองได้ในเวลาไม่นาน", False),
    ("TH3", "core", "I ask questions when I do not understand, instead of guessing.", "ฉันถามเมื่อไม่เข้าใจ แทนที่จะเดาเอาเอง", False),
    ("TH3", "staff", "I read, watch or try things to learn new knowledge for my job.", "ฉันอ่าน ดู หรือทดลองเพื่อหาความรู้ใหม่ที่ใช้ในงาน", False),
    ("TH4", "core", "I suggest ideas to make my work safer, easier or faster.", "ฉันเสนอความคิดเพื่อให้งานปลอดภัยขึ้น ง่ายขึ้น หรือเร็วขึ้น", False),
    ("TH4", "core", "I prefer to keep doing the work the old way, even when a better way is shown.", "ฉันชอบทำงานแบบเดิม แม้มีคนแนะนำวิธีที่ดีกว่า", True),
    ("TH4", "staff", "I test an improvement, measure the result and share it so others can use it.", "ฉันทดลองการปรับปรุง วัดผล และแบ่งปันให้ผู้อื่นนำไปใช้", False),
    ("PE1", "core", "When I hand over work, I tell the next person clearly what was done and what is left.", "เมื่อส่งต่องาน ฉันบอกคนถัดไปอย่างชัดเจนว่าทำอะไรไปแล้วและเหลืออะไร", False),
    ("PE1", "core", "I listen until the other person has finished before I answer.", "ฉันฟังจนอีกฝ่ายพูดจบก่อนจึงตอบ", False),
    ("PE1", "staff", "My emails, reports and messages are short, correct and easy to act on.", "อีเมล รายงาน และข้อความของฉันสั้น ถูกต้อง และนำไปใช้ได้ง่าย", False),
    ("PE2", "core", "When my part is done, I help colleagues who are behind.", "เมื่องานของฉันเสร็จ ฉันช่วยเพื่อนที่งานยังไม่เสร็จ", False),
    ("PE2", "core", "I treat colleagues with respect, whatever their nationality, age or job.", "ฉันให้เกียรติเพื่อนร่วมงาน ไม่ว่าจะเป็นสัญชาติ อายุ หรือตำแหน่งใด", False),
    ("PE2", "staff", "In a disagreement, I talk it through calmly to find a solution that works for the team.", "เมื่อเห็นต่าง ฉันพูดคุยอย่างใจเย็นเพื่อหาทางออกที่ดีต่อทีม", False),
    ("PE3", "core", "I work well with people from other sections when the job needs it.", "ฉันทำงานกับคนจากแผนกอื่นได้ดีเมื่องานต้องการ", False),
    ("PE3", "staff", "I build good relationships with other departments before I need their help.", "ฉันสร้างความสัมพันธ์ที่ดีกับแผนกอื่นไว้ก่อนที่จะต้องขอความช่วยเหลือ", False),
    ("PE3", "staff", "I can agree a plan with people who do not report to me and have other priorities.", "ฉันตกลงแผนงานกับคนที่ไม่ได้อยู่ใต้บังคับบัญชาและมีงานสำคัญอื่นได้", False),
    ("PE4", "core", "I think about the next process before I send my work on.", "ฉันคำนึงถึงกระบวนการถัดไปก่อนส่งงานต่อ", False),
    ("PE4", "core", "When the next process tells me about a problem, I act on it quickly.", "เมื่อกระบวนการถัดไปแจ้งปัญหา ฉันแก้ไขอย่างรวดเร็ว", False),
    ("PE4", "staff", "I know what our customers and the next department need and use it in my work.", "ฉันรู้ว่าลูกค้าและหน่วยงานถัดไปต้องการอะไร และนำมาใช้ในงานของฉัน", False),
    ("SE1", "core", "I come to work on time and follow the shift plan.", "ฉันมาทำงานตรงเวลาและทำตามแผนกะ", False),
    ("SE1", "core", "When I make a mistake, I report it at once instead of hiding it.", "เมื่อทำผิดพลาด ฉันแจ้งทันทีแทนที่จะปิดบัง", False),
    ("SE1", "staff", "I do what I said I would do, by the time I said.", "ฉันทำสิ่งที่รับปากไว้ ตามเวลาที่บอกไว้", False),
    ("SE2", "core", "When the line stops or there is a rush order, I stay calm and keep working carefully.", "เมื่อไลน์หยุดหรือมีงานเร่ง ฉันยังใจเย็นและทำงานอย่างระมัดระวัง", False),
    ("SE2", "core", "Under pressure, I get angry or give up easily.", "เมื่อถูกกดดัน ฉันโกรธหรือท้อได้ง่าย", True),
    ("SE2", "staff", "After a bad day, I recover quickly and focus on what I can change.", "หลังจากวันที่แย่ ฉันฟื้นตัวได้เร็วและมุ่งที่สิ่งที่ฉันเปลี่ยนแปลงได้", False),
    ("SE3", "core", "I record weights, times and counts exactly as they are, even if the number looks bad.", "ฉันบันทึกน้ำหนัก เวลา และจำนวนตามจริง แม้ตัวเลขจะดูไม่ดี", False),
    ("SE3", "core", "I use company time, materials and tools only for company work.", "ฉันใช้เวลา วัตถุดิบ และเครื่องมือของบริษัทเพื่องานของบริษัทเท่านั้น", False),
    ("SE3", "staff", "I speak up when I see something dishonest, even if it is uncomfortable.", "ฉันแจ้งเมื่อพบสิ่งที่ไม่ซื่อสัตย์ แม้จะรู้สึกอึดอัด", False),
    ("SE4", "core", "I ask my supervisor or colleagues how I can do my job better.", "ฉันถามหัวหน้าหรือเพื่อนว่าจะทำงานให้ดีขึ้นได้อย่างไร", False),
    ("SE4", "core", "I want to learn a new skill or a new machine this year.", "ฉันอยากเรียนทักษะใหม่หรือเครื่องจักรใหม่ในปีนี้", False),
    ("SE4", "staff", "I have a clear goal for my own development and I work on it.", "ฉันมีเป้าหมายการพัฒนาตนเองที่ชัดเจนและลงมือทำ", False),
    ("DE1", "core", "I check my work against the standard before sending it on.", "ฉันตรวจงานตามมาตรฐานก่อนส่งต่อ", False),
    ("DE1", "core", "If I find a defect, I stop and report it, even if it slows the line.", "เมื่อพบของเสีย ฉันหยุดและแจ้ง แม้จะทำให้ไลน์ช้าลง", False),
    ("DE1", "staff", "I know the quality targets for my area and check them myself.", "ฉันรู้เป้าหมายคุณภาพของพื้นที่ตนเองและตรวจสอบด้วยตนเอง", False),
    ("DE2", "core", "I wear the right PPE for the job, every time.", "ฉันสวมอุปกรณ์ PPE ที่ถูกต้องทุกครั้ง", False),
    ("DE2", "core", "I warn a colleague who is working unsafely, or tell the supervisor.", "ฉันเตือนเพื่อนที่ทำงานไม่ปลอดภัย หรือแจ้งหัวหน้า", False),
    ("DE2", "core", "I sometimes skip a safety step to save time.", "บางครั้งฉันข้ามขั้นตอนความปลอดภัยเพื่อประหยัดเวลา", True),
    ("DE3", "core", "I know my daily target and work to reach it.", "ฉันรู้เป้าหมายรายวันของตนเองและทำงานให้ถึงเป้า", False),
    ("DE3", "core", "When something blocks my work, I find a way or ask for help quickly.", "เมื่อมีอุปสรรคต่องาน ฉันหาทางแก้หรือขอความช่วยเหลืออย่างรวดเร็ว", False),
    ("DE3", "staff", "I set myself goals above the minimum and track my progress.", "ฉันตั้งเป้าหมายสูงกว่าขั้นต่ำและติดตามความก้าวหน้าของตนเอง", False),
    ("DE4", "core", "I avoid wasting rubber, water, electricity and packing materials.", "ฉันหลีกเลี่ยงการสูญเสียยาง น้ำ ไฟฟ้า และวัสดุบรรจุภัณฑ์", False),
    ("DE4", "core", "I look after machines and tools so they last longer.", "ฉันดูแลเครื่องจักรและเครื่องมือให้ใช้งานได้นาน", False),
    ("DE4", "staff", "I know the main costs in my area and look for ways to reduce them.", "ฉันรู้ต้นทุนหลักในพื้นที่ของตนเองและหาวิธีลดต้นทุน", False),
    ("LE1", "leader", "I teach new people step by step and check that they can do it alone.", "ฉันสอนคนใหม่ทีละขั้นและตรวจว่าเขาทำเองได้", False),
    ("LE1", "leader", "I give feedback soon after I see good or poor work, with a clear example.", "ฉันให้คำแนะนำไม่นานหลังเห็นงานที่ดีหรือไม่ดี พร้อมตัวอย่างที่ชัดเจน", False),
    ("LE1", "leader", "I know each team member's skill level and plan who to train next.", "ฉันรู้ระดับทักษะของสมาชิกแต่ละคนและวางแผนว่าจะสอนใครต่อ", False),
    ("LE2", "leader", "My team knows the goal for the day and their own part in it.", "ทีมของฉันรู้เป้าหมายของวันและหน้าที่ของแต่ละคน", False),
    ("LE2", "leader", "I share OT, hard jobs and chances fairly within the team.", "ฉันแบ่ง OT งานหนัก และโอกาสอย่างเป็นธรรมในทีม", False),
    ("LE2", "leader", "I notice when someone is unhappy or struggling and talk with them.", "ฉันสังเกตเมื่อมีคนไม่สบายใจหรือมีปัญหา และพูดคุยกับเขา", False),
    ("LE3", "leader", "I make decisions on time, even when not all information is available.", "ฉันตัดสินใจทันเวลา แม้ข้อมูลยังไม่ครบทั้งหมด", False),
    ("LE3", "leader", "I explain the reasons for my decisions to the people affected.", "ฉันอธิบายเหตุผลของการตัดสินใจให้ผู้ที่ได้รับผลกระทบ", False),
    ("LE3", "leader", "I avoid decisions and wait for my manager to decide.", "ฉันหลีกเลี่ยงการตัดสินใจและรอให้ผู้บังคับบัญชาตัดสินใจ", True),
    ("LE4", "leader", "When a new rule or method comes, I explain why it matters before asking people to change.", "เมื่อมีกฎหรือวิธีใหม่ ฉันอธิบายว่าทำไมจึงสำคัญก่อนขอให้คนเปลี่ยน", False),
    ("LE4", "leader", "I follow up after a change until it becomes the normal way of working.", "ฉันติดตามหลังการเปลี่ยนแปลงจนกลายเป็นวิธีทำงานปกติ", False),
    ("LE4", "leader", "I bring new ideas from outside the team and try them.", "ฉันนำความคิดใหม่จากภายนอกทีมมาทดลองใช้", False),
]

# code, English, Thai, short description EN, Thai, targets {competency: level 1-5}
GROWTH_PATHS = [
    ("TEAMLEAD", "Team leader", "หัวหน้าทีม",
     "Leads a shift or team: people, safety and the daily target.",
     "นำกะหรือทีม ดูแลคน ความปลอดภัย และเป้าหมายรายวัน",
     {"LE2": 4, "LE1": 4, "LE3": 4, "PE1": 4, "PE2": 4, "SE1": 4, "DE2": 4, "DE3": 4}),
    ("EXPERT", "Technical expert", "ผู้เชี่ยวชาญด้านเทคนิค",
     "Goes deep in one area (machines, lab, process) and is the one others ask.",
     "เชี่ยวชาญลึกในด้านหนึ่ง (เครื่องจักร แล็บ กระบวนการ) และเป็นที่พึ่งของผู้อื่น",
     {"TH1": 4, "TH3": 4, "DE1": 4, "SE4": 4, "PE1": 3, "LE1": 3, "SE3": 4}),
    ("IMPROVE", "Improvement champion", "ผู้นำการปรับปรุงงาน",
     "Finds better ways of working and gets them adopted across the factory.",
     "หาวิธีทำงานที่ดีกว่าและผลักดันให้ใช้ทั่วโรงงาน",
     {"TH4": 4, "TH1": 4, "DE4": 4, "PE3": 4, "LE4": 4, "TH3": 4}),
    ("GUARDIAN", "Quality and safety guardian", "ผู้พิทักษ์คุณภาพและความปลอดภัย",
     "Protects people and product: standards, checks and stopping unsafe work.",
     "ปกป้องคนและสินค้า ด้วยมาตรฐาน การตรวจสอบ และการหยุดงานที่ไม่ปลอดภัย",
     {"DE1": 4, "DE2": 5, "SE1": 4, "SE3": 4, "PE1": 4, "SE2": 4}),
    ("PARTNER", "Coordinator and trainer", "ผู้ประสานงานและผู้สอนงาน",
     "Connects teams and helps people learn: onboarding, training and service.",
     "เชื่อมทีมต่างๆ และช่วยให้คนเรียนรู้ ทั้งการรับคนใหม่ การอบรม และการบริการ",
     {"PE1": 4, "PE3": 4, "PE4": 4, "LE1": 4, "PE2": 4, "TH2": 4}),
]


def seed_competency(db: Session) -> None:
    for i, (code, cl, en, th, den, dth, lead) in enumerate(COMPETENCIES):
        if db.get(Competency, code) is None:
            db.add(Competency(code=code, cluster=cl, name_en=en, name_th=th, definition_en=den, definition_th=dth, leaders_only=lead, sort_order=i))
    db.flush()
    if not db.query(CompetencyQuestion).count():
        for i, (comp, aud, en, th, rev) in enumerate(QUESTIONS):
            db.add(CompetencyQuestion(competency_code=comp, audience=aud, text_en=en, text_th=th, reverse=rev, sort_order=i))
    for code, en, th, den, dth, targets in GROWTH_PATHS:
        if db.get(GrowthPath, code) is None:
            db.add(GrowthPath(code=code, name_en=en, name_th=th, description_en=den, description_th=dth, targets=targets))
    db.flush()
