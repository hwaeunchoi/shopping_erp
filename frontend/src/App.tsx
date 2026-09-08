import { Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthContext'
import { ProtectedRoute } from './components/ProtectedRoute'
import { Layout } from './components/Layout'
import { LoginPage } from './pages/LoginPage'
import { DashboardPage } from './pages/DashboardPage'
import { OrdersPage } from './pages/OrdersPage'
import { OrderDetailPage } from './pages/OrderDetailPage'
import { ShipmentsPage } from './pages/ShipmentsPage'
import { FulfillmentPage } from './pages/FulfillmentPage'
import { CsCasesPage } from './pages/CsCasesPage'
import { OrderConflictsPage } from './pages/OrderConflictsPage'
import { ExchangesPage } from './pages/ExchangesPage'
import { ReturnsPage } from './pages/ReturnsPage'
import { CancellationsPage } from './pages/CancellationsPage'
import { ProductsPage } from './pages/ProductsPage'
import { ProductDetailPage } from './pages/ProductDetailPage'
import { ProductBulkPage } from './pages/ProductBulkPage'
import { UnmatchedItemsPage } from './pages/UnmatchedItemsPage'
import { CustomersPage } from './pages/CustomersPage'
import { InventoryPage } from './pages/InventoryPage'
import { SuppliersPage } from './pages/SuppliersPage'
import { PurchaseOrdersPage } from './pages/PurchaseOrdersPage'
import { SettlementsPage } from './pages/SettlementsPage'
import { CostsPage } from './pages/CostsPage'
import { AdsPage } from './pages/AdsPage'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { ReportsPage } from './pages/ReportsPage'
import { SystemMonitoringPage } from './pages/SystemMonitoringPage'
import { NotificationsPage } from './pages/NotificationsPage'
import { SettingsPage } from './pages/SettingsPage'

function Protected({ children }: { children: React.ReactNode }) {
  return (
    <ProtectedRoute>
      <Layout>{children}</Layout>
    </ProtectedRoute>
  )
}

function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<Protected><DashboardPage /></Protected>} />
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
      </Routes>
    </AuthProvider>
  )
}

export default App
