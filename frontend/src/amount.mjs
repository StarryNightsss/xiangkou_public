export function yuanToCents(value) {
  const text=String(value).trim()
  if (!/^(0|[1-9]\d{0,5})(\.\d{1,2})?$/.test(text)) throw new Error('金额需为非负数，最多两位小数')
  const [whole,fraction='']=text.split('.')
  const cents=Number(whole)*100+Number(fraction.padEnd(2,'0'))
  if(cents>10000000) throw new Error('单张券金额最多100000元')
  return cents
}
