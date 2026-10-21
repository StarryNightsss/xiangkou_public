import test from 'node:test'
import assert from 'node:assert/strict'
import {yuanToCents} from '../src/amount.mjs'
test('yuan forms preserve cents exactly including free coupons',()=>{assert.equal(yuanToCents('89.01'),8901);assert.equal(yuanToCents('0'),0);assert.equal(yuanToCents('0.1'),10)})
test('no rounding of extra decimals or non monetary inputs',()=>{for(const x of ['-1','1.001','1e3','NaN','','100000.01'])assert.throws(()=>yuanToCents(x))})
