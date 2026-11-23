/** 欢迎页问候语池：按时间段分组的亲近问候 + 通用问候，随机且避免连续重复。 */

const GREETINGS = {
  morning: [
    '早上好，今天想从哪里开始？',
    '早安，书里还是网上，先查点什么？',
    '早上好，有什么想弄明白的？',
  ],
  afternoon: [
    '下午好，想读点什么，还是想理清什么？',
    '午后正好，想问点什么？',
    '下午好，需要我帮你查查吗？',
  ],
  evening: [
    '晚上好，慢慢想，我不着急。',
    '夜深了，想聊点什么？',
    '晚上好，今天有什么收获想整理？',
  ],
  any: [
    '有什么可以帮你？',
    '书里、网上，或手边的事，都可以问我。',
    '今天想探索什么？',
    '想到什么就问，我来帮你找答案。',
  ],
}

function bucketFor(hour) {
  if (hour < 6) return 'evening'
  if (hour < 12) return 'morning'
  if (hour < 18) return 'afternoon'
  return 'evening'
}

/** 纯函数：给定时间与上一条问候，返回一条不重复的问候语。 */
export function pickGreeting(date = new Date(), last = '') {
  const pool = [...GREETINGS[bucketFor(date.getHours())], ...GREETINGS.any].filter(
    (text) => text !== last
  )
  const list = pool.length ? pool : GREETINGS.any
  return list[Math.floor(Math.random() * list.length)]
}
