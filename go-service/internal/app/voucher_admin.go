package app

import (
	"context"
	"strconv"
	"strings"
	"time"

	"xiangkou/go-service/internal/domain"
	"xiangkou/go-service/internal/platform/errs"
)

type VoucherAdminRepo interface {
	InsertVoucher(ctx context.Context, v domain.VoucherBrief, seckill bool, stock int, begin, end string) (int64, error)
	DelistVoucher(ctx context.Context, id int64) (bool, error)
	GetVoucherBrief(ctx context.Context, id int64) (*domain.VoucherBrief, error)
}

// VoucherAdmin 演示用的商品管理：只有配置里的管理员 id 能建券/下架。
type VoucherAdmin struct {
	repo     VoucherAdminRepo
	adminIDs map[int64]bool
	demoIDs  map[int64]bool
}

// NewVoucherAdmin 区分两级权限：admin 可建券与下架；demo 只能建券（演示账号，避免误删）。
func NewVoucherAdmin(repo VoucherAdminRepo, adminIDs, demoIDs string) *VoucherAdmin {
	parse := func(raw string) map[int64]bool {
		allowed := make(map[int64]bool)
		for _, part := range strings.Split(raw, ",") {
			if id, err := strconv.ParseInt(strings.TrimSpace(part), 10, 64); err == nil && id > 0 {
				allowed[id] = true
			}
		}
		return allowed
	}
	return &VoucherAdmin{repo: repo, adminIDs: parse(adminIDs), demoIDs: parse(demoIDs)}
}

func (a *VoucherAdmin) canManage(userID int64) bool { return a.adminIDs[userID] }

func (a *VoucherAdmin) canCreate(userID int64) bool {
	return a.adminIDs[userID] || a.demoIDs[userID]
}

// Capabilities 返回当前账号是否可管理商品，以及服务端时间/时区。
func (a *VoucherAdmin) Capabilities(userID int64) map[string]any {
	now := time.Now()
	zone := now.Location().String()
	return map[string]any{
		"can_create_voucher": a.canCreate(userID),
		"server_time":        now.Format("2006-01-02T15:04:05.000"),
		"server_zone":        zone,
	}
}

// CreateRequest 对应前端提交的券表单。
type CreateRequest struct {
	ShopID      int64  `json:"shopId"`
	Title       string `json:"title"`
	SubTitle    string `json:"subTitle"`
	Rules       string `json:"rules"`
	PayValue    int64  `json:"payValue"`
	ActualValue int64  `json:"actualValue"`
	Stock       int    `json:"stock"`
	BeginTime   string `json:"beginTime"`
	EndTime     string `json:"endTime"`
}

func (a *VoucherAdmin) validate(userID int64, req CreateRequest, seckill bool) (domain.VoucherBrief, error) {
	voucher := domain.VoucherBrief{}
	if !a.canCreate(userID) {
		return voucher, errs.New(1000, 200, "当前账号没有演示商品管理权限")
	}
	if req.ShopID <= 0 || strings.TrimSpace(req.Title) == "" || len([]rune(req.Title)) > 100 ||
		strings.TrimSpace(req.Rules) == "" || len([]rune(req.Rules)) > 1000 || len([]rune(req.SubTitle)) > 200 {
		return voucher, errs.New(1000, 200, "标题或规则无效")
	}
	if req.PayValue < 0 || req.ActualValue <= 0 || req.ActualValue > 10000000 || req.PayValue > req.ActualValue {
		return voucher, errs.New(1000, 200, "券金额无效")
	}
	if seckill && (req.Stock <= 0 || req.Stock > 100000 || req.BeginTime == "" || req.EndTime == "" || req.EndTime <= req.BeginTime) {
		return voucher, errs.New(1000, 200, "秒杀库存或时间无效")
	}
	title := strings.TrimSpace(req.Title)
	if !strings.HasPrefix(title, "【演示】") {
		title = "【演示】" + title
	}
	if len([]rune(title)) > 100 {
		return voucher, errs.New(1000, 200, "标题过长")
	}
	voucher.ShopID = req.ShopID
	voucher.Title = title
	voucher.Rules = strings.TrimSpace(req.Rules)
	voucher.PayValue = req.PayValue
	voucher.ActualValue = req.ActualValue
	if seckill {
		voucher.Type = 1
	} else {
		voucher.Type = 0
	}
	return voucher, nil
}

func (a *VoucherAdmin) Create(ctx context.Context, userID int64, req CreateRequest, seckill bool) (int64, error) {
	voucher, err := a.validate(userID, req, seckill)
	if err != nil {
		return 0, err
	}
	return a.repo.InsertVoucher(ctx, voucher, seckill, req.Stock, req.BeginTime, req.EndTime)
}

func (a *VoucherAdmin) Delete(ctx context.Context, userID, id int64) error {
	if !a.canManage(userID) {
		return errs.New(1000, 200, "当前账号没有演示商品管理权限")
	}
	voucher, err := a.repo.GetVoucherBrief(ctx, id)
	if err != nil {
		return err
	}
	if voucher == nil {
		return errs.New(1000, 200, "优惠券不存在")
	}
	_, err = a.repo.DelistVoucher(ctx, id)
	return err
}
