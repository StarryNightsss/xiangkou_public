package app

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"math/big"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"xiangkou/go-service/internal/domain"
	"xiangkou/go-service/internal/infra/mail"
	"xiangkou/go-service/internal/platform/errs"
)

const (
	loginCodeTTL = 5 * time.Minute
	// loginUserTTL 是登录态滑动过期时间：演示场景放宽到 24 小时，避免放着不动就被踢下线。
	loginUserTTL    = 24 * time.Hour
	loginCodeCool   = 60 * time.Second
	loginFailWindow = 10 * time.Minute
	loginFailLimit  = 5
)

type UserReader interface {
	FindUserByEmail(ctx context.Context, email string) (*domain.User, error)
	CreateUser(ctx context.Context, email, nickName string) (*domain.User, error)
	GetUser(ctx context.Context, id int64) (*domain.User, error)
	GetUserInfo(ctx context.Context, userID int64) (*domain.UserInfo, error)
	ListBlogsByIDs(ctx context.Context, ids []int64) ([]domain.Blog, error)
}

type Identity struct {
	users  UserReader
	rdb    *redis.Client
	mailer mail.Sender
}

func NewIdentity(users UserReader, rdb *redis.Client, mailer mail.Sender) *Identity {
	return &Identity{users: users, rdb: rdb, mailer: mailer}
}

// SendCode 本地演示模式不发邮件，验证码写 Redis 供 redis-cli 取用（与 既有服务 行为一致）。
func (i *Identity) SendCode(ctx context.Context, email string) error {
	email = strings.TrimSpace(email)
	if email == "" {
		return errs.New(1000, 200, "请填写邮箱")
	}
	// 同一邮箱 60 秒内只能请求一次，避免被刷。
	limited, err := i.rdb.SetNX(ctx, "login:code:limit:"+email, "1", loginCodeCool).Result()
	if err != nil {
		return err
	}
	if !limited {
		return errs.New(1000, 200, "请求过于频繁，请稍后再试")
	}
	code := randomDigits(6)
	if err := i.rdb.Set(ctx, "login:code:"+email, code, loginCodeTTL).Err(); err != nil {
		return err
	}
	if i.mailer != nil && i.mailer.Enabled() {
		body := "您的登录验证码是：" + code + "，5 分钟内有效。请勿转发给他人。"
		if err := i.mailer.Send(ctx, email, "巷口 登录验证码", body); err != nil {
			// 码已写入 Redis，便于本地救援；但对外必须报失败，不能假装成功。
			return errs.New(1000, 200, "邮件发送失败，请稍后重试")
		}
	}
	return nil
}

func (i *Identity) Login(ctx context.Context, email, code string) (string, error) {
	email = strings.TrimSpace(email)
	if email == "" {
		return "", errs.New(1000, 200, "账号不能为空！")
	}
	failKey := "login:code:fail:" + email
	if fails, _ := i.rdb.Get(ctx, failKey).Int(); fails >= loginFailLimit {
		return "", errs.New(1000, 200, "尝试次数过多，请稍后再试")
	}
	cached, err := i.rdb.Get(ctx, "login:code:"+email).Result()
	if err != nil || cached != strings.TrimSpace(code) {
		i.rdb.Incr(ctx, failKey)
		i.rdb.Expire(ctx, failKey, loginFailWindow)
		return "", errs.New(1000, 200, "验证码错误")
	}
	// 登录成功后验证码立即失效，避免 5 分钟内重放。
	i.rdb.Del(ctx, "login:code:"+email, failKey)
	user, err := i.users.FindUserByEmail(ctx, email)
	if err != nil {
		return "", err
	}
	if user == nil {
		user, err = i.users.CreateUser(ctx, email, "user_"+randomDigits(8))
		if err != nil {
			return "", err
		}
	}
	return i.issueToken(ctx, user)
}

func (i *Identity) GuestLogin(ctx context.Context) (string, error) {
	guestEmail := "guest@xiangkou.local"
	user, err := i.users.FindUserByEmail(ctx, guestEmail)
	if err != nil {
		return "", err
	}
	if user == nil {
		user, err = i.users.CreateUser(ctx, guestEmail, "游客_"+randomDigits(4))
		if err != nil {
			return "", err
		}
	}
	return i.issueToken(ctx, user)
}

func (i *Identity) Logout(ctx context.Context, token string) error {
	if strings.TrimSpace(token) == "" {
		return errs.New(1000, 200, "缺少登录令牌")
	}
	return i.rdb.Del(ctx, "login:token:"+token).Err()
}

func (i *Identity) Me(ctx context.Context, userID int64) (*domain.UserDTO, error) {
	if userID == 0 {
		return nil, nil
	}
	user, err := i.users.GetUser(ctx, userID)
	if err != nil || user == nil {
		return nil, err
	}
	return &domain.UserDTO{ID: user.ID, NickName: user.NickName, Icon: user.Icon}, nil
}

func (i *Identity) UserByID(ctx context.Context, id int64) (*domain.UserDTO, error) {
	user, err := i.users.GetUser(ctx, id)
	if err != nil || user == nil {
		return nil, err
	}
	return &domain.UserDTO{ID: user.ID, NickName: user.NickName, Icon: user.Icon}, nil
}

func (i *Identity) Info(ctx context.Context, id int64) (*domain.UserInfo, error) {
	return i.users.GetUserInfo(ctx, id)
}

// Sign 用 bitmap 记录当月签到：key=sign:{userId}:yyyyMM，位下标=日-1。
func (i *Identity) Sign(ctx context.Context, userID int64) error {
	if userID == 0 {
		return errs.ErrUnauthorized
	}
	now := time.Now()
	key := fmt.Sprintf("sign:%d:%s", userID, now.Format("200601"))
	return i.rdb.SetBit(ctx, key, int64(now.Day()-1), 1).Err()
}

// SignCount 统计从今天往前连续签到的天数。
func (i *Identity) SignCount(ctx context.Context, userID int64) (int, error) {
	if userID == 0 {
		return 0, errs.ErrUnauthorized
	}
	now := time.Now()
	key := fmt.Sprintf("sign:%d:%s", userID, now.Format("200601"))
	day := now.Day()
	values, err := i.rdb.BitField(ctx, key, "GET", "u"+strconv.Itoa(day), 0).Result()
	if err != nil || len(values) == 0 || values[0] == 0 {
		return 0, nil
	}
	bits := values[0]
	count := 0
	for index := 0; index < day; index++ {
		if bits&(1<<uint(index)) == 0 {
			break
		}
		count++
	}
	return count, nil
}

func (i *Identity) issueToken(ctx context.Context, user *domain.User) (string, error) {
	buffer := make([]byte, 16)
	if _, err := rand.Read(buffer); err != nil {
		return "", err
	}
	token := hex.EncodeToString(buffer)
	key := "login:token:" + token
	pipe := i.rdb.Pipeline()
	pipe.HSet(ctx, key, map[string]any{
		"id": strconv.FormatInt(user.ID, 10), "nickName": user.NickName, "icon": user.Icon,
	})
	pipe.Expire(ctx, key, loginUserTTL)
	if _, err := pipe.Exec(ctx); err != nil {
		return "", err
	}
	return token, nil
}

func randomDigits(length int) string {
	const digits = "0123456789"
	out := make([]byte, length)
	for index := range out {
		value, err := rand.Int(rand.Reader, big.NewInt(int64(len(digits))))
		if err != nil {
			out[index] = '0'
			continue
		}
		out[index] = digits[value.Int64()]
	}
	return string(out)
}
