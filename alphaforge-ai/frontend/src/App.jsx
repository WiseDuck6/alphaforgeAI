import { useEffect, useState } from 'react'
import { Route, Routes } from 'react-router-dom'
import { api } from './api.js'
import Layout from './components/Layout.jsx'
import { UserProvider } from './UserContext.jsx'
import Dashboard from './pages/Dashboard.jsx'
import ProfileWizard from './pages/ProfileWizard.jsx'
import Assets from './pages/Assets.jsx'
import Holdings from './pages/Holdings.jsx'
import AssetDetail from './pages/AssetDetail.jsx'
import Ask from './pages/Ask.jsx'
import Monitoring from './pages/Monitoring.jsx'
import DataLimits from './pages/DataLimits.jsx'

export default function App() {
  const [placeholder, setPlaceholder] = useState(false)
  useEffect(() => {
    api.dataStatus().then((s) => setPlaceholder(s.is_placeholder)).catch(() => null)
  }, [])
  return (
    <UserProvider>
      <Layout placeholder={placeholder}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/profile" element={<ProfileWizard />} />
          <Route path="/holdings" element={<Holdings />} />
          <Route path="/assets" element={<Assets />} />
          <Route path="/assets/:ticker" element={<AssetDetail />} />
          <Route path="/ask" element={<Ask />} />
          <Route path="/monitoring" element={<Monitoring />} />
          <Route path="/data" element={<DataLimits />} />
          <Route path="*" element={<p className="text-slate-500">Page not found.</p>} />
        </Routes>
      </Layout>
    </UserProvider>
  )
}
