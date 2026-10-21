export interface PersonalOrder {
  order_id: string
  title?: string
  voucher_title?: string
  voucherTitle?: string
  orderId?: string
  status: number
}

export interface TaskCheckpoint {
  run_id: string
  session_id: string
  phase: string
  updated_at: number
}

export interface ActionState {
  action_id: string
  state: string
  order_id: string | null
  order_status?: number
  request_state?: string
}
