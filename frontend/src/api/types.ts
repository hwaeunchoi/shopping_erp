// api/types.ts
// 백엔드 api/routers/*.py의 Pydantic 응답 스키마와 1:1로 대응하는 타입 정의.

export interface TokenResponse {
  access_token: string
  token_type: string
}

export interface UserProfile {
  id: number
  username: string
  name: string
  role_id: number
  is_active: boolean
  theme_preference: string
  permissions: string[]
}

export interface Platform {
  id: number
  code: string
  name: string
  connector_class: string
  settlement_cycle_days: number | null
  is_active: boolean
}

export interface Product {
  id: number
  name: string
  category: string | null
  brand: string | null
  manufacturer: string | null
  base_price: number | null
  status: string
  is_deleted: boolean
}

export interface ProductCreate {
  name: string
  category?: string | null
  brand?: string | null
  manufacturer?: string | null
  base_price?: number | null
  status?: string
}

export interface ProductUpdate {
  name?: string
  category?: string | null
  brand?: string | null
  manufacturer?: string | null
  base_price?: number | null
  status?: string
}

export interface ProductOption {
  id: number
  product_id: number
  sku_code: string
  option_name: string | null
  color: string | null
  size: string | null
  barcode: string | null
  unit_cost_price: number | null
  sale_price: number | null
  is_active: boolean
  sort_order: number
}

export interface ProductOptionCreate {
  sku_code: string
  option_name?: string | null
  color?: string | null
  size?: string | null
  barcode?: string | null
  unit_cost_price?: number | null
  sale_price?: number | null
}

export interface ProductOptionUpdate {
  option_name?: string | null
  color?: string | null
  size?: string | null
  barcode?: string | null
  unit_cost_price?: number | null
  sale_price?: number | null
}

// platform_option_id = 플랫폼 옵션번호(네이버 channelProductNo 등, 매핑 키/유니크).
// platform_product_id = 플랫폼 상품번호(네이버 groupProductNo 등, 비유니크/참고용).
export interface ProductPlatformMap {
  id: number
  product_option_id: number
  platform_id: number
  platform_option_id: string
  platform_product_id: string | null
  display_name: string | null
  seller_product_code: string | null
}

export interface ProductPlatformMapCreate {
  platform_id: number
  platform_option_id: string
  platform_product_id?: string | null
  display_name?: string | null
  seller_product_code?: string | null
}

export interface ProductPlatformMapUpdate {
  display_name?: string | null
  seller_product_code?: string | null
  platform_product_id?: string | null
  platform_option_id?: string | null
}

export interface ProductImage {
  id: number
  product_id: number
  image_url: string
  is_thumbnail: boolean
  sort_order: number
}

export interface ProductImageCreate {
  image_url: string
  is_thumbnail?: boolean
}

export interface UnmatchedPlatformItem {
  id: number
  platform_id: number
  platform_order_no: string | null
  platform_option_id: string | null
  platform_product_id: string | null
  product_name: string | null
  option_name: string | null
  seller_product_code: string | null
  quantity: number | null
  unit_price: number | null
  status: string
  matched_by: string | null
  matched_at: string | null
  created_at: string
}

export interface ProductOptionStats {
  total_quantity_sold: number
  last_order_date: string | null
  current_cost_price: number | null
  sellable_stock: number
}

export interface ProductOptionDetail extends ProductOption {
  platform_maps: ProductPlatformMap[]
  stats: ProductOptionStats
}

export interface ProductDetail extends Product {
  options: ProductOptionDetail[]
  images: ProductImage[]
}

export interface ProductCostHistory {
  id: number
  product_option_id: number
  supplier_id: number | null
  cost_price: number
  effective_from: string
  effective_to: string | null
}

export interface ProductCostHistoryCreate {
  cost_price: number
  effective_from: string
  supplier_id?: number | null
}

export interface Customer {
  id: number
  platform_id: number
  name: string | null
  phone: string | null
  grade: string | null
  is_vip: boolean
  is_dormant: boolean
  total_purchase_amount: number
  order_count: number
}

export interface Order {
  id: number
  platform_id: number
  platform_order_no: string
  customer_id: number | null
  status: string
  order_date: string
  total_amount: number
  discount_amount: number
  platform_name: string | null
  customer_name: string | null
}

export interface OrderRow {
  id: number
  platform_id: number
  platform_name: string | null
  platform_order_no: string
  order_status: string
  payment_status: string
  shipping_status: string
  cs_status: string
  order_date: string
  payment_date: string | null
  delivery_completed_date: string | null
  is_delayed: boolean
  customer_name: string | null
  customer_phone: string | null
  customer_address: string | null
  product_name: string | null
  option_name: string | null
  sku_code: string | null
  channel_product_no: string | null
  channel_option_no: string | null
  item_count: number
  total_quantity: number
  supplier_name: string | null
  carrier: string | null
  tracking_no: string | null
  total_amount: number
  assignee_id: number | null
  assignee_name: string | null
  tags: string | null
}

export interface OrderListResponse {
  rows: OrderRow[]
  total: number
  page: number
  page_size: number
}

export interface OrderDetailItem {
  id: number
  product_option_id: number
  product_name: string | null
  option_name: string | null
  sku_code: string | null
  channel_product_no: string | null
  channel_option_no: string | null
  quantity: number
  unit_price: number
  line_amount: number
}

export interface OrderDetailInventory {
  sku_code: string | null
  warehouse_id: number
  sellable_stock: number
  reserved_stock: number
  available_stock: number
  safety_stock: number
}

export interface OrderDetailBundle {
  id: number
  platform_order_no: string
  platform_name: string | null
  order_status: string
  payment_status: string
  shipping_status: string
  cs_status: string
  order_date: string
  payment_date: string | null
  delivery_completed_date: string | null
  total_amount: number
  discount_amount: number
  customer: {
    id: number
    name: string | null
    phone: string | null
    email: string | null
    address: string | null
    grade: string | null
    is_vip: boolean
    order_count: number
  } | null
  items: OrderDetailItem[]
  inventory: OrderDetailInventory[]
  shipment: {
    carrier: string | null
    tracking_no: string | null
    status: string
    shipped_at: string | null
    delivered_at: string | null
  } | null
  memos: { id: number; content: string; created_by: number | null; created_at: string }[]
  status_history: { from_status: string | null; to_status: string; changed_at: string }[]
  assignee_id: number | null
  assignee_name: string | null
  tags: string | null
  purchase_links: { purchase_order_id: number; supplier_name: string | null; status: string; order_date: string | null }[]
  contribution: {
    sale_amount: number
    cost_of_goods: number
    platform_fee: number
    contribution_margin: number
    contribution_rate: number
    ad_cost_note: string
  }
  settlement: {
    settlement_id: number
    settlement_cycle: string | null
    status: string | null
    gross_amount: number
    fee_amount: number
    net_amount: number
  } | null
  cs_history: { type: string; id: number; reason: string | null; status: string; requested_at: string }[]
}

export interface BulkResult {
  succeeded: number
  failed: number
  errors: string[]
}

export interface OrderItem {
  id: number
  product_option_id: number
  quantity: number
  unit_price: number
  line_amount: number
  product_name: string | null
  option_name: string | null
  sku_code: string | null
}

export interface OrderDetail extends Order {
  items: OrderItem[]
  customer_name: string | null
  customer_phone: string | null
}

export interface OrderMemo {
  id: number
  target_type: string
  target_id: number
  content: string
  created_by: number | null
  created_at: string
}

export interface OrderSyncResult {
  total: number
  created: number
  updated: number
  skipped_items: number
  auto_matched_products: number
}

export interface ProductNaverSyncResult {
  total_items: number
  created_products: number
  created_options: number
  updated_options: number
}

export interface OrderRate {
  order_count: number
  exchange_count: number
  exchange_rate: number
  return_count: number
  return_rate: number
  cancellation_count: number
  cancellation_rate: number
}

export interface OrderAlerts {
  unshipped_count: number
  delayed_unshipped_count: number
  exchange_pending_count: number
  return_pending_count: number
  cancellation_pending_count: number
}

export interface InventoryItem {
  id: number
  product_option_id: number
  warehouse_id: number
  sellable_stock: number
  reserved_stock: number
  safety_stock: number
  updated_at: string
  sku_code: string | null
  option_name: string | null
  product_name: string | null
  warehouse_name: string | null
}

export interface InventoryAdjustRequest {
  product_option_id: number
  warehouse_id: number
  delta: number
  memo?: string | null
}

export interface InventorySafetyStockRequest {
  product_option_id: number
  warehouse_id: number
  safety_stock: number
}

export interface InventoryTransaction {
  id: number
  product_option_id: number
  warehouse_id: number
  type: string
  quantity: number
  reference_type: string | null
  reference_id: number | null
  memo: string | null
  created_at: string
}

export interface Supplier {
  id: number
  name: string
  business_no: string | null
  bank_name: string | null
  bank_account_no: string | null
  bank_account_holder: string | null
  payment_terms: string | null
  is_active: boolean
}

export interface SupplierCreateRequest {
  name: string
  business_no?: string | null
  bank_name?: string | null
  bank_account_no?: string | null
  bank_account_holder?: string | null
  payment_terms?: string | null
}

export interface SupplierUpdateRequest {
  name?: string
  business_no?: string | null
  bank_name?: string | null
  bank_account_no?: string | null
  bank_account_holder?: string | null
  payment_terms?: string | null
  is_active?: boolean
}

export interface SupplierContact {
  id: number
  supplier_id: number
  name: string
  phone: string | null
  email: string | null
  is_primary: boolean
}

export interface SupplierContactRequest {
  name: string
  phone?: string | null
  email?: string | null
  is_primary?: boolean
}

export interface PurchaseOrderItem {
  id: number
  purchase_order_id: number
  product_option_id: number
  quantity: number
  unit_cost: number
  received_quantity: number
}

export interface PurchaseOrder {
  id: number
  supplier_id: number
  status: 'DRAFT' | 'ORDERED' | 'PARTIALLY_RECEIVED' | 'RECEIVED' | 'CANCELLED'
  order_date: string | null
  memo: string | null
  items: PurchaseOrderItem[]
}

export interface PurchaseOrderCreateRequest {
  supplier_id: number
  memo?: string | null
}

export interface PurchaseOrderItemCreateRequest {
  product_option_id: number
  quantity: number
  unit_cost?: number
}

export interface PurchaseOrderReceiveRequest {
  quantity: number
  warehouse_id: number
}

export interface Settlement {
  id: number
  platform_id: number
  settlement_cycle: string
  status: string
  expected_amount: number
  settled_amount: number
  unsettled_amount: number
}

export interface SettlementDetail {
  id: number
  order_id: number
  gross_amount: number
  fee_amount: number
  net_amount: number
}

export interface Cost {
  id: number
  category: string
  cost_type: string
  platform_id: number | null
  product_option_id: number | null
  order_id: number | null
  amount: number
  incurred_date: string
  memo: string | null
}

export interface CostCreate {
  category: string
  cost_type: string
  platform_id?: number | null
  product_option_id?: number | null
  order_id?: number | null
  amount: number
  incurred_date: string
  memo?: string | null
}

export interface AdCampaign {
  id: number
  ad_platform_code: string
  platform_campaign_id: string
  name: string | null
  product_option_id: number | null
  is_active: boolean
}

export interface AdPerformance {
  stat_date: string
  impressions: number
  clicks: number
  cost: number
  conversions: number
  conversion_amount: number
  cpc: number
  cpm: number
  ctr: number
  roas: number
}

export interface ProfitLossSummary {
  period_type: string
  basis_type: string
  period_key: string
  platform_id: number | null
  gross_revenue: number
  net_revenue: number
  order_count: number
  ad_cost: number
  cost_of_goods: number
  platform_fee: number
  total_cost: number
  net_profit: number
  net_profit_rate: number
}

export interface ProductPerformance {
  product_option_id: number
  product_id: number | null
  sales_qty: number
  revenue: number
  net_profit: number
  ad_cost: number
  roas: number
}

// --- 배송/교환/반품/취소 관리 (1순위) ---

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface Shipment {
  id: number
  order_id: number
  carrier: string | null
  tracking_no: string | null
  shipped_at: string | null
  delivered_at: string | null
  status: string
}

export interface ShipmentCreate {
  order_id: number
  carrier?: string | null
  tracking_no?: string | null
}

export interface ShipmentInfoUpdate {
  carrier?: string | null
  tracking_no?: string | null
}

export const SHIPMENT_STATUSES = ['READY', 'SHIPPING', 'DELIVERED'] as const

// 상용 ERP 확장(1단계) - 채널 상태와 내부 상태가 허용되지 않은 전이로 어긋난 경우의 충돌 기록.
export interface OrderStatusConflict {
  id: number
  order_id: number
  internal_status: string
  channel_status: string
  detected_at: string
  resolved_at: string | null
  resolution: string | null
}

export interface Exchange {
  id: number
  order_id: number
  order_item_id: number | null
  reason: string | null
  status: string
  requested_at: string
  completed_at: string | null
}

export interface ExchangeCreate {
  order_id: number
  order_item_id?: number | null
  reason?: string | null
}

export const EXCHANGE_STATUSES = ['REQUESTED', 'APPROVED', 'SHIPPED', 'COMPLETED', 'REJECTED'] as const

export interface Return {
  id: number
  order_id: number
  order_item_id: number | null
  reason: string | null
  refund_amount: number | null
  status: string
  requested_at: string
  completed_at: string | null
}

export interface ReturnCreate {
  order_id: number
  order_item_id?: number | null
  reason?: string | null
  refund_amount?: number | null
}

export const RETURN_STATUSES = ['REQUESTED', 'APPROVED', 'RECEIVED', 'REFUNDED', 'REJECTED'] as const

export interface Cancellation {
  id: number
  order_id: number
  reason: string | null
  refund_amount: number | null
  status: string
  requested_at: string
  completed_at: string | null
}

export interface CancellationCreate {
  order_id: number
  reason?: string | null
  refund_amount?: number | null
}

export const CANCELLATION_STATUSES = ['REQUESTED', 'COMPLETED'] as const

export interface StatusUpdate {
  status: string
  warehouse_id?: number | null
}

// --- UI v1.1 addendum: 통합검색/즐겨찾기/최근조회/시스템모니터링/AI Assistant ---

export interface SearchResultItem {
  type: string
  id: number
  label: string
  sublabel: string
}

export interface SearchResults {
  orders: SearchResultItem[]
  products: SearchResultItem[]
  product_options: SearchResultItem[]
  customers: SearchResultItem[]
  shipments: SearchResultItem[]
}

export interface Favorite {
  id: number
  target_type: string
  target_id: number
  created_at: string
}

export interface RecentView {
  id: number
  target_type: string
  target_id: number
}

export interface IntegrationStatus {
  id: number
  integration_type: string
  integration_code: string
  status: string
  last_success_at: string | null
  last_error_at: string | null
  last_error_message: string | null
  token_expires_at: string | null
  updated_at: string
}

export interface TaskExecutionHistoryItem {
  id: number
  task_type: string
  target: string | null
  trigger_type: string
  triggered_by: number | null
  status: string
  started_at: string
  finished_at: string | null
  result_summary: string | null
  error_message: string | null
}

export interface LatestBackupInfo {
  created_at: string
  status: string
  file_size_bytes: number | null
}

export interface SystemStatus {
  db_size_bytes: number
  log_dir_size_bytes: number
  latest_backup: LatestBackupInfo | null
  uptime_seconds: number
}

export interface AiAssistantAnswer {
  answer: string
}

export interface TaskTriggerResult {
  id: number
  task_type: string
  status: string
  result_summary: string | null
  error_message: string | null
}

// --- UI v1.0 알림센터 ---

export interface AppNotification {
  id: number
  rule_id: number | null
  type: string
  severity: string
  message: string
  is_read: boolean
  created_at: string
}

export interface AlertRule {
  id: number
  name: string
  metric: string
  operator: string
  threshold_value: number | null
  scope_type: string | null
  scope_id: number | null
  check_frequency: string
  is_enabled: boolean
  created_by: number | null
  created_at: string
}

export interface AlertRuleCreate {
  name: string
  metric: string
  operator: string
  threshold_value?: number | null
  check_frequency?: string
  is_enabled?: boolean
}

// --- 설정(Settings) 화면: 사용자/역할/권한/API Credential/시스템설정 ---

export interface SettingsUser {
  id: number
  username: string
  name: string
  email: string | null
  role_id: number
  is_active: boolean
}

export interface SettingsUserCreate {
  username: string
  password: string
  name: string
  role_id: number
  email?: string | null
}

export interface SettingsUserUpdate {
  name?: string
  email?: string | null
  role_id?: number
  is_active?: boolean
  password?: string
}

export interface SettingsRole {
  id: number
  name: string
  description: string | null
}

export interface SettingsRoleCreate {
  name: string
  description?: string | null
}

export interface SettingsPermission {
  id: number
  code: string
  name: string
  menu_group: string | null
}

export interface RolePermissions {
  role_id: number
  permission_codes: string[]
}

export interface ApiCredentialMasked {
  id: number
  owner_type: string
  owner_id: number
  key_name: string
  masked_value: string
  expires_at: string | null
  updated_at: string
}

export interface ApiCredentialUpsert {
  owner_type: string
  owner_id: number
  key_name: string
  plain_value: string
  expires_at?: string | null
}

export interface SystemSetting {
  id: number
  category: string
  key: string
  value: string | null
  updated_at: string
}

// --- SRS 3.7 종합 보고서 ---

export interface MonthlyReportProfitLoss {
  gross_revenue: number
  net_revenue: number
  order_count: number
  ad_cost: number
  ad_conversion_revenue: number
  cost_of_goods: number
  platform_fee: number
  shipping_cost: number
  packaging_cost: number
  other_cost: number
  total_cost: number
  net_profit: number
  net_profit_rate: number
}

export interface ReportPlatformBreakdown {
  platform_id: number
  platform_code: string
  platform_name: string
  revenue: number
  order_count: number
}

export interface ReportProductPerformance {
  product_option_id: number
  sales_qty: number
  revenue: number
  net_profit: number
  ad_cost: number
  roas: number
}

export interface ReportAdPerformance {
  ad_platform_code: string
  impressions: number
  clicks: number
  cost: number
  conversions: number
  conversion_amount: number
  cpc: number
  cpm: number
  roas: number
}

export interface ReportKpiComparison {
  metric: string
  target_value: number
  actual_value: number
  achievement_rate: number
}

export interface ReportCostTypeBreakdown {
  fixed_cost: number
  variable_cost: number
}

export interface MonthlyReport {
  period_key: string
  profit_loss: MonthlyReportProfitLoss
  platform_breakdown: ReportPlatformBreakdown[]
  best_products: ReportProductPerformance[]
  worst_products: ReportProductPerformance[]
  ad_performance: ReportAdPerformance[]
  return_rate: number
  exchange_rate: number
  cancel_rate: number
  cost_type_breakdown: ReportCostTypeBreakdown
  kpi_comparisons: ReportKpiComparison[]
}

export interface ReportSchedule {
  id: number
  report_type: string
  frequency: string
  output_format: string
  recipient_emails: string | null
  is_enabled: boolean
  next_run_at: string
  last_run_at: string | null
  created_by: number | null
  created_at: string
}

export interface ReportScheduleCreate {
  report_type: string
  frequency: string
  output_format: string
  recipient_emails?: string | null
  next_run_at: string
  is_enabled?: boolean
}
