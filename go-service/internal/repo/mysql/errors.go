package mysql

import (
	"errors"

	"github.com/go-sql-driver/mysql"

	"xiangkou/go-service/internal/domain"
)

// asDuplicate 把驱动返回的 1062 唯一键冲突转成领域错误，交由用例层决定是「返回已有订单」还是「ACK 消息」。
func asDuplicate(err error) error {
	var mysqlErr *mysql.MySQLError
	if errors.As(err, &mysqlErr) && mysqlErr.Number == 1062 {
		return domain.ErrDuplicateOrder
	}
	return err
}
