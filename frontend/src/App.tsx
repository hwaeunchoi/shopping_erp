import { lazy, Suspense, type ComponentType } from 'react'
import { Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthContext'
import { ProtectedRoute } from './components/ProtectedRoute'
import { Layout } from './components/Layout'

// 화면(route) 단위로 코드를 나눠 처음 접속할 때 내려받는 JS 를 줄인다. 이름 있는 export 를 그대로 쓴다.
function lazyPage<T extends Record<string, ComponentType>>(load: () => Promise<T>, name: keyof T & string) {
  return lazy(() => load().then((m) => ({ default: m[name] })))
}

const LoginPage = lazyPage(() => import('./pages/LoginPage'), 'LoginPage')
const DashboardPage = lazyPage(() => import('./pages/DashboardPage'), 'DashboardPage')
const OperationsDashboardPage = lazyPage(() => import('./pages/OperationsDashboardPage'), 'OperationsDashboardPage')
const OrdersPage = lazyPage(() => import('./pages/OrdersPage'), 'OrdersPage')
const OrderDetailPage = lazyPage(() => import('./pages/OrderDetailPage'), 'OrderDetailPage')
const ShipmentsPage = lazyPage(() => import('./pages/ShipmentsPage'), 'ShipmentsPage')
const FulfillmentPage = lazyPage(() => import('./pages/FulfillmentPage'), 'FulfillmentPage')
const CsCasesPage = lazyPage(() => import('./pages/CsCasesPage'), 'CsCasesPage')
const OrderConflictsPage = lazyPage(() => import('./pages/OrderConflictsPage'), 'OrderConflictsPage')
const ExchangesPage = lazyPage(() => import('./pages/ExchangesPage'), 'ExchangesPage')
const ReturnsPage = lazyPage(() => import('./pages/ReturnsPage'), 'ReturnsPage')
const CancellationsPage = lazyPage(() => import('./pages/CancellationsPage'), 'CancellationsPage')
const ProductsPage = lazyPage(() => import('./pages/ProductsPage'), 'ProductsPage')
const ProductDetailPage = lazyPage(() => import('./pages/ProductDetailPage'), 'ProductDetailPage')
const ProductBulkPage = lazyPage(() => import('./pages/ProductBulkPage'), 'ProductBulkPage')
const UnmatchedItemsPage = lazyPage(() => import('./pages/UnmatchedItemsPage'), 'UnmatchedItemsPage')
const CustomersPage = lazyPage(() => import('./pages/CustomersPage'), 'CustomersPage')
const InventoryPage = lazyPage(() => import('./pages/InventoryPage'), 'InventoryPage')
const SuppliersPage = lazyPage(() => import('./pages/SuppliersPage'), 'SuppliersPage')
const PurchaseOrdersPage = lazyPage(() => import('./pages/PurchaseOrdersPage'), 'PurchaseOrdersPage')
const SettlementsPage = lazyPage(() => import('./pages/SettlementsPage'), 'SettlementsPage')
const CostsPage = lazyPage(() => import('./pages/CostsPage'), 'CostsPage')
const AdsPage = lazyPage(() => import('./pages/AdsPage'), 'AdsPage')
const AnalyticsPage = lazyPage(() => import('./pages/AnalyticsPage'), 'AnalyticsPage')
const ReportsPage = lazyPage(() => import('./pages/ReportsPage'), 'ReportsPage')
const SystemMonitoringPage = lazyPage(() => import('./pages/SystemMonitoringPage'), 'SystemMonitoringPage')
const NotificationsPage = lazyPage(() => import('./pages/NotificationsPage'), 'NotificationsPage')
const SettingsPage = lazyPage(() => import('./pages/SettingsPage'), 'SettingsPage')
const NotFoundPage = lazyPage(() => import('./pages/NotFoundPage'), 'NotFoundPage')

function PageLoading() {
  return (
    <div className="page-loading" role="status">
      화면을 불러오는 중입니다...
    </div>
  )
}

function Protected({ children }: { children: React.ReactNode }) {
  return (
    <ProtectedRoute>
      <Layout>
        <Suspense fallback={<PageLoading />}>{children}</Suspense>
      </Layout>
    </ProtectedRoute>
  )
}

function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<Suspense fallback={<PageLoading />}><LoginPage /></Suspense>} />
        <Route path="/" element={<Protected><DashboardPage /></Protected>} />
        <Route path="/operations" element={<Protected><OperationsDashboardPage /></Protected>} />
        <Route path="/orders" element={<Protected><OrdersPage /></Protected>} />
        <Route path="/orders/:orderId" element={<Protected><OrderDetailPage /></Protected>} />
        <Route path="/shipments" element={<Protected><ShipmentsPage /></Protected>} />
        <Route path="/fulfillment" element={<Protected><FulfillmentPage /></Protected>} />
        <Route path="/cs-cases" element={<Protected><CsCasesPage /></Protected>} />
        <Route path="/order-conflicts" element={<Protected><OrderConflictsPage /></Protected>} />
        <Route path="/exchanges" element={<Protected><ExchangesPage /></Protected>} />
        <Route path="/returns" element={<Protected><ReturnsPage /></Protected>} />
        <Route path="/cancellations" element={<Protected><CancellationsPage /></Protected>} />
        <Route path="/products" element={<Protected><ProductsPage /></Protected>} />
        <Route path="/products/:productId" element={<Protected><ProductDetailPage /></Protected>} />
        <Route path="/products-bulk" element={<Protected><ProductBulkPage /></Protected>} />
        <Route path="/unmatched-items" element={<Protected><UnmatchedItemsPage /></Protected>} />
        <Route path="/customers" element={<Protected><CustomersPage /></Protected>} />
        <Route path="/inventory" element={<Protected><InventoryPage /></Protected>} />
        <Route path="/suppliers" element={<Protected><SuppliersPage /></Protected>} />
        <Route path="/purchase-orders" element={<Protected><PurchaseOrdersPage /></Protected>} />
        <Route path="/settlements" element={<Protected><SettlementsPage /></Protected>} />
        <Route path="/costs" element={<Protected><CostsPage /></Protected>} />
        <Route path="/ads" element={<Protected><AdsPage /></Protected>} />
        <Route path="/analytics" element={<Protected><AnalyticsPage /></Protected>} />
        <Route path="/reports" element={<Protected><ReportsPage /></Protected>} />
        <Route path="/system-monitoring" element={<Protected><SystemMonitoringPage /></Protected>} />
        <Route path="/notifications" element={<Protected><NotificationsPage /></Protected>} />
        <Route path="/settings" element={<Protected><SettingsPage /></Protected>} />
        <Route path="*" element={<Protected><NotFoundPage /></Protected>} />
      </Routes>
    </AuthProvider>
  )
}

export default App
