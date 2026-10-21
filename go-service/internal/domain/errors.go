package domain

import "errors"

// ErrDuplicateOrder 由数据访问层返回：唯一索引判定「该用户对这张券已有未取消订单」。
var ErrDuplicateOrder = errors.New("domain: duplicate order")
