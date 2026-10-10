package api

import (
	"net/http"
	"strconv"
)

func (s *Server) operations(w http.ResponseWriter, r *http.Request) {
	u := user(r)
	if u.Role != "admin" {
		fail(w, 403, "需要管理员权限")
		return
	}
	q := r.URL.Query()
	for key := range q {
		if key != "days" {
			fail(w, 400, "不支持该统计参数")
			return
		}
	}
	if len(q["days"]) > 1 {
		fail(w, 400, "统计窗口重复")
		return
	}
	days := 7
	if q.Get("days") != "" {
		var err error
		days, err = strconv.Atoi(q.Get("days"))
		if err != nil {
			fail(w, 400, "统计窗口无效")
			return
		}
	}
	if days != 1 && days != 7 && days != 30 {
		fail(w, 400, "统计窗口只能为 1、7、30 天")
		return
	}
	result, err := s.DB.Operations(r.Context(), u.TenantID, days)
	if err != nil {
		fail(w, 503, "运行统计暂时不可用")
		return
	}
	write(w, 200, result)
}
