# QuantConnect Project Type: Classic Algorithm (Python)

from AlgorithmImports import *
import numpy as np
from datetime import timedelta
from scipy.optimize import minimize

class RiskParityStrategy(QCAlgorithm):
    """
    Risk Parity Strategy with Dynamic Correlation Adjustment
    Implements equal risk contribution portfolio with leverage,
    correlation regime detection, and tail risk hedging.
    """

    def Initialize(self):
        self.SetStartDate(2016, 1, 1)
        self.SetEndDate(2024, 12, 31)
        self.SetCash(1000000)

        # Asset classes for risk parity
        self.assets = {
            # Equities
            "SPY": {"class": "equity", "sub": "us_large"},
            "QQQ": {"class": "equity", "sub": "us_tech"},
            "EFA": {"class": "equity", "sub": "intl_dev"},
            "EEM": {"class": "equity", "sub": "intl_em"},

            # Fixed Income
            "TLT": {"class": "fixed_income", "sub": "lt_treasury"},
            "IEF": {"class": "fixed_income", "sub": "it_treasury"},
            "LQD": {"class": "fixed_income", "sub": "ig_credit"},
            "TIP": {"class": "fixed_income", "sub": "tips"},

            # Commodities
            "GLD": {"class": "commodities", "sub": "gold"},
            "DBC": {"class": "commodities", "sub": "broad"},

            # Real Assets
            "VNQ": {"class": "real_assets", "sub": "reits"},
        }

        self.symbols = {}
        for ticker, info in self.assets.items():
            symbol = self.AddEquity(ticker, Resolution.Daily).Symbol
            self.symbols[ticker] = symbol

        # Leverage and risk parameters
        self.target_volatility = 0.10  # 10% target vol
        self.max_leverage = 1.5
        self.lookback_volatility = 60
        self.lookback_correlation = 120
        self.min_weight = 0.02
        self.max_weight = 0.30

        # Correlation regime
        self.correlation_regime = "normal"  # normal, elevated, crisis

        # Tail risk hedge
        self.hedge_active = False
        self.spy_sma = self.SMA(self.symbols["SPY"], 200, Resolution.Daily)

        # State tracking
        self.last_weights = {}
        self.last_rebalance = self.Time

        self.SetWarmUp(timedelta(days=210))

        # Monthly rebalancing
        self.Schedule.On(
            self.DateRules.MonthStart("SPY"),
            self.TimeRules.AfterMarketOpen("SPY", 30),
            self.Rebalance
        )

        # Weekly correlation check
        self.Schedule.On(
            self.DateRules.WeekStart("SPY"),
            self.TimeRules.AfterMarketOpen("SPY", 15),
            self.UpdateCorrelationRegime
        )

    def OnData(self, data):
        if self.IsWarmingUp:
            return

    def Rebalance(self):
        if self.IsWarmingUp:
            return

        # Get returns matrix
        returns_matrix = self.GetReturnsMatrix()
        if returns_matrix is None:
            return

        # Calculate covariance matrix
        cov_matrix = self.CalculateCovarianceMatrix(returns_matrix)

        # Calculate risk parity weights
        rp_weights = self.CalculateRiskParityWeights(cov_matrix)

        # Adjust for correlation regime
        adjusted_weights = self.AdjustForCorrelationRegime(rp_weights, cov_matrix)

        # Apply volatility scaling
        scaled_weights = self.ApplyVolatilityScaling(adjusted_weights, cov_matrix)

        # Check for tail risk hedge
        if self.ShouldActivateHedge():
            scaled_weights = self.ApplyTailRiskHedge(scaled_weights)

        # Execute trades
        self.ExecuteTrades(scaled_weights)

        self.last_weights = scaled_weights
        self.last_rebalance = self.Time

    def GetReturnsMatrix(self):
        """
        Get returns matrix for all assets
        """
        lookback = max(self.lookback_volatility, self.lookback_correlation) + 10

        returns_dict = {}

        for ticker, symbol in self.symbols.items():
            history = self.History(symbol, lookback, Resolution.Daily)

            if history.empty or len(history) < self.lookback_volatility:
                self.Log(f"Insufficient data for {ticker}")
                return None

            prices = history['close'].values
            returns = np.diff(prices) / prices[:-1]
            returns_dict[ticker] = returns[-self.lookback_correlation:]

        # Convert to matrix
        min_length = min(len(r) for r in returns_dict.values())
        returns_matrix = np.array([returns_dict[t][-min_length:] for t in self.symbols.keys()])

        return returns_matrix

    def CalculateCovarianceMatrix(self, returns_matrix):
        """
        Calculate shrunk covariance matrix (Ledoit-Wolf)
        """
        n_assets, n_obs = returns_matrix.shape

        # Sample covariance
        sample_cov = np.cov(returns_matrix)

        # Shrinkage target: diagonal matrix with average variance
        avg_var = np.trace(sample_cov) / n_assets
        target = np.eye(n_assets) * avg_var

        # Shrinkage intensity (simplified Ledoit-Wolf)
        shrinkage = 0.2  # Fixed shrinkage for simplicity

        # Shrunk covariance
        shrunk_cov = shrinkage * target + (1 - shrinkage) * sample_cov

        # Annualize
        shrunk_cov *= 252

        return shrunk_cov

    def CalculateRiskParityWeights(self, cov_matrix):
        """
        Calculate risk parity (equal risk contribution) weights
        """
        n_assets = len(self.symbols)

        def portfolio_volatility(weights, cov):
            return np.sqrt(weights @ cov @ weights)

        def risk_contribution(weights, cov):
            port_vol = portfolio_volatility(weights, cov)
            marginal_contrib = cov @ weights
            risk_contrib = weights * marginal_contrib / port_vol
            return risk_contrib

        def objective(weights):
            rc = risk_contribution(weights, cov_matrix)
            target_rc = np.ones(n_assets) / n_assets
            return np.sum((rc - target_rc) ** 2)

        # Constraints and bounds
        constraints = [
            {'type': 'eq', 'fun': lambda w: np.sum(w) - 1}
        ]
        bounds = [(self.min_weight, self.max_weight) for _ in range(n_assets)]

        # Initial guess: equal weight
        x0 = np.ones(n_assets) / n_assets

        # Optimize
        result = minimize(
            objective,
            x0,
            method='SLSQP',
            bounds=bounds,
            constraints=constraints,
            options={'maxiter': 1000}
        )

        if result.success:
            weights = result.x
        else:
            # Fallback to inverse volatility
            vols = np.sqrt(np.diag(cov_matrix))
            inv_vols = 1 / vols
            weights = inv_vols / np.sum(inv_vols)

        # Map back to tickers
        weight_dict = {ticker: weights[i] for i, ticker in enumerate(self.symbols.keys())}

        return weight_dict

    def UpdateCorrelationRegime(self):
        """
        Detect correlation regime (correlations tend to spike in crises)
        """
        returns_matrix = self.GetReturnsMatrix()
        if returns_matrix is None:
            return

        # Calculate recent correlation matrix
        corr_matrix = np.corrcoef(returns_matrix[:, -30:])

        # Average off-diagonal correlation
        n = corr_matrix.shape[0]
        avg_corr = (np.sum(np.abs(corr_matrix)) - n) / (n * n - n)

        # Compare to historical
        full_corr_matrix = np.corrcoef(returns_matrix)
        historical_avg_corr = (np.sum(np.abs(full_corr_matrix)) - n) / (n * n - n)

        # Regime determination
        if avg_corr > historical_avg_corr * 1.5:
            self.correlation_regime = "crisis"
        elif avg_corr > historical_avg_corr * 1.2:
            self.correlation_regime = "elevated"
        else:
            self.correlation_regime = "normal"

        self.Log(f"Correlation regime: {self.correlation_regime}, Avg corr: {avg_corr:.3f}")

    def AdjustForCorrelationRegime(self, weights, cov_matrix):
        """
        Adjust weights based on correlation regime
        """
        adjusted = weights.copy()

        if self.correlation_regime == "crisis":
            # Reduce equity, increase gold and treasuries
            equity_reduction = 0.7
            safe_haven_boost = 1.5

            for ticker in adjusted:
                asset_class = self.assets[ticker]["class"]
                if asset_class == "equity":
                    adjusted[ticker] *= equity_reduction
                elif ticker in ["GLD", "TLT", "IEF"]:
                    adjusted[ticker] *= safe_haven_boost

        elif self.correlation_regime == "elevated":
            # Slight reduction in risky assets
            for ticker in adjusted:
                asset_class = self.assets[ticker]["class"]
                if asset_class == "equity":
                    adjusted[ticker] *= 0.9

        # Normalize
        total = sum(adjusted.values())
        if total > 0:
            for ticker in adjusted:
                adjusted[ticker] /= total

        return adjusted

    def ApplyVolatilityScaling(self, weights, cov_matrix):
        """
        Scale portfolio to target volatility
        """
        # Calculate portfolio volatility
        weight_array = np.array([weights[t] for t in self.symbols.keys()])
        port_vol = np.sqrt(weight_array @ cov_matrix @ weight_array)

        if port_vol > 0:
            scale_factor = self.target_volatility / port_vol
            scale_factor = np.clip(scale_factor, 0.5, self.max_leverage)
        else:
            scale_factor = 1.0

        scaled = {t: w * scale_factor for t, w in weights.items()}

        # Cap individual positions
        for ticker in scaled:
            scaled[ticker] = min(scaled[ticker], self.max_weight * self.max_leverage)

        self.Log(f"Portfolio vol: {port_vol:.2%}, Scale factor: {scale_factor:.2f}")

        return scaled

    def ShouldActivateHedge(self):
        """
        Check if tail risk hedge should be activated
        """
        if not self.spy_sma.IsReady:
            return False

        spy_price = self.Securities[self.symbols["SPY"]].Price
        spy_sma = self.spy_sma.Current.Value

        # Activate hedge if SPY below 200-day SMA
        if spy_price < spy_sma * 0.98:
            if not self.hedge_active:
                self.Log("Tail risk hedge ACTIVATED")
            self.hedge_active = True
            return True
        else:
            if self.hedge_active:
                self.Log("Tail risk hedge DEACTIVATED")
            self.hedge_active = False
            return False

    def ApplyTailRiskHedge(self, weights):
        """
        Reduce risk exposure during hedge activation
        """
        hedged = weights.copy()

        # Reduce overall exposure
        for ticker in hedged:
            asset_class = self.assets[ticker]["class"]

            if asset_class == "equity":
                hedged[ticker] *= 0.3  # Reduce equity by 70%
            elif asset_class in ["commodities", "real_assets"]:
                hedged[ticker] *= 0.5
            elif ticker in ["TLT", "IEF"]:
                hedged[ticker] *= 1.3  # Increase treasuries
            elif ticker == "GLD":
                hedged[ticker] *= 1.5  # Increase gold

        return hedged

    def ExecuteTrades(self, target_weights):
        """
        Execute trades with smooth rebalancing
        """
        portfolio_value = self.Portfolio.TotalPortfolioValue

        for ticker, symbol in self.symbols.items():
            target_weight = target_weights.get(ticker, 0)
            current_weight = self.Portfolio[symbol].HoldingsValue / portfolio_value if portfolio_value > 0 else 0

            # Only trade if significant deviation
            if abs(current_weight - target_weight) > 0.02:
                self.SetHoldings(symbol, target_weight)

        # Log portfolio
        total_exposure = sum(target_weights.values())
        self.Log(f"Total exposure: {total_exposure:.2f}x")

        # Log by asset class
        class_weights = {}
        for ticker, weight in target_weights.items():
            asset_class = self.assets[ticker]["class"]
            class_weights[asset_class] = class_weights.get(asset_class, 0) + weight

        for asset_class, weight in class_weights.items():
            self.Log(f"  {asset_class}: {weight:.2%}")