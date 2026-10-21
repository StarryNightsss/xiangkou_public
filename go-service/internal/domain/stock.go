package domain

// SeckillStock 对账用的秒杀券库存：数据库为基准，Redis 向它收敛。
type SeckillStock struct {
	VoucherID int64
	Stock     int
}
