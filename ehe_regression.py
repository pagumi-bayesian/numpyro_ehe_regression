# %%
import numpy as np
import pandas as pd
import arviz as az

import jax
import jax.numpy as jnp
import jax.random as random

import numpyro
import numpyro.distributions as dist
from numpyro.distributions.transforms import (
    AffineTransform,
    ExpTransform,
    PowerTransform,
)
from numpyro.contrib.funsor import config_enumerate
from numpyro.infer import Predictive, init_to_median
from numpyro.infer import MCMC, NUTS

# MCMCを4chain並列に回すための設定
numpyro.set_host_device_count(4)


# %%
class EHERegression:
    def __init__(self, model_type: str = "ehe") -> None:
        if model_type not in ["ehe", "normal"]:
            raise ValueError("引数model_typeには、'ehe'か'normal'を指定してください。")
        self.model_type = model_type

        if self.model_type == "ehe":
            self.model = self.model_ehe
        elif self.model_type == "normal":
            self.model == self.model_normal

    @config_enumerate
    def model_ehe(y: jnp.array = None, x: jnp.array = None):
        # 外れ値かどうかを決定する確率pの事前分布として、ベータ分布を使用
        p = numpyro.sample("p", dist.Beta(1, 4))

        # 指数分布にしたがうgammaパラメータをサンプリング
        gamma = numpyro.sample("gamma", dist.Exponential(1))

        # 外れ値の誤差項の分散uの事前分布を定義
        # パラメータgammaをもつパレート分布に複数の変換を行う
        u = numpyro.sample(
            "u",
            dist.TransformedDistribution(
                base_distribution=dist.Pareto(1, gamma),
                transforms=[
                    AffineTransform(loc=-1, scale=1),
                    ExpTransform(),
                    AffineTransform(loc=-1, scale=1),
                ],
            ),
        )
        # 誤差項の標準偏差(外れ値でなければ1, 外れ値であればsqrt(u))
        scale_eps = jnp.array([1.0, jnp.sqrt(u)])

        # 全誤差項で共通の尺度sigmaは、逆ガンマ分布をベースにし平方根をとる
        sigma = numpyro.sample(
            "sigma",
            dist.TransformedDistribution(
                base_distribution=dist.InverseGamma(1, 1),
                transforms=[PowerTransform(exponent=0.5)],
            ),
        )

        # 切片alphaの事前分布
        alpha = numpyro.sample("alpha", dist.Normal(0, 10))

        # 係数betaの事前分布
        beta = numpyro.sample(
            "beta",
            dist.Normal(jnp.zeros(x.shape[1]), 10 * jnp.ones(x.shape[1])).to_event(1),
        )

        # 観測点ごとに繰り返し処理を行うためのplate構造
        N = x.shape[0]
        with numpyro.plate("data", N):
            # 確率pのベルヌーイ分布から、外れ値フラグis_outlierをサンプリング
            is_outlier = numpyro.sample("is_outlier", dist.Bernoulli(probs=p))

            # 平均値muを計算
            mu = numpyro.deterministic("mu", alpha + jnp.dot(x, beta))

            # 正規分布に基づいて、観測値yをサンプリング
            # スケールは外れ値かどうかで変える
            y = numpyro.sample(
                "y", dist.Normal(mu, sigma * scale_eps[is_outlier]), obs=y
            )

    def model_normal(y: jnp.array = None, x: jnp.array = None):
        # 全誤差項で共通の尺度sigmaは、逆ガンマ分布をベースにし平方根をとる
        sigma = numpyro.sample(
            "sigma",
            dist.TransformedDistribution(
                base_distribution=dist.InverseGamma(1, 1),
                transforms=[PowerTransform(exponent=0.5)],
            ),
        )

        # 切片alphaの事前分布
        alpha = numpyro.sample("alpha", dist.Normal(0, 10))

        # 係数betaの事前分布
        beta = numpyro.sample(
            "beta",
            dist.Normal(jnp.zeros(x.shape[1]), 10 * jnp.ones(x.shape[1])).to_event(1),
        )

        # 観測点ごとに繰り返し処理を行うためのplate構造
        N = x.shape[0]
        with numpyro.plate("data", N):
            # 平均値muを計算
            mu = numpyro.deterministic("mu", alpha + jnp.dot(x, beta))

            # 正規分布に基づいて、観測値yをサンプリング
            # スケールは外れ値かどうかで変える
            y = numpyro.sample("y", dist.Normal(mu, sigma), obs=y)

    def fit(
        self,
        x: jnp.array,
        y: jnp.array,
        num_warmup: int = 1000,
        num_samples: int = 4000,
        thinning: int = 1,
        num_chains: int = 4,
        seed: int = 123,
    ):
        """MCMCを実行

        x: 説明変数
        y: 目的変数
        num_warmup: バーンインの数
        num_samples: MCMCで生成するサンプルの数
        thinning: 間引きの間隔
        num_chains: MCMCのチェイン数
        seed: 乱数シード
        """

        rng_key = random.PRNGKey(seed)
        kernel = NUTS(self.model, init_strategy=init_to_median(num_samples=20))

        # progress_bar=Falseにすると、計算スピードがアップするらしい
        mcmc = MCMC(
            kernel,
            num_warmup=num_warmup,
            num_samples=num_samples,
            thinning=thinning,
            num_chains=num_chains,
            progress_bar=True,
        )

        self.y = y
        self.x = x
        mcmc.run(rng_key, y=y, x=x)

        self.mcmc_samples = mcmc.get_samples(group_by_chain=False)

        inference_data = az.from_dict(mcmc.get_samples(group_by_chain=True))
        self.inference_data = inference_data

        mcmc.print_summary()

        return None

    def plot_trace(self, ax=None):
        if self.model_type == "ehe":
            var_names = ["alpha", "beta", "sigma", "p", "gamma", "u"]
        elif self.model_type == "normal":
            var_names = ["alpha", "beta", "sigma"]

        trace_plot = az.plot_trace(
            self.inference_data,
            combined=False,
            var_names=var_names,
            # グラフの文字が被らないようにする設定
            # 参考：https://toeming.hatenablog.com/entry/2022/06/05/ArviZ_GetInShape
            backend_kwargs={"constrained_layout": True},
            axes=ax,
        )

        return trace_plot

    def generate_obs_prediction(
        self,
        prediction_type: str = "posterior",
        return_mean_hdi_df: bool = True,
        hdi_prob: float = 0.9,
        x_predictor: jnp.array = None,
        num_samples: int = 1000,
        seed: int = 123,
    ):
        """観測値yの予測分布からデータを生成する

        prediction_type: "posterior"を指定した場合は事後分布から、"prior"を指定した場合は事前分布からサンプルを生成
        return_mean_hdi_df: 生成したサンプルから算出した平均およびHDIをまとめたデータフレームを返すか
        hdi_prob: HDIの広さを定義(return_mean_hdi_df=Trueの場合にのみ使用)
        predictor: 説明変数x(Noneの場合、MCMCに使用した説明変数が使われる)
        num_samples: 生成するサンプルの数(prediction_type="prior"の場合にのみ使用)
        seed: 乱数シード
        """
        rng_key = random.PRNGKey(seed)

        if x_predictor == None:
            x_predictor = self.x

        # 事後分布から生成する場合
        if prediction_type == "posterior":
            predictive = Predictive(self.model, self.mcmc_samples)
            prediction = predictive(rng_key, x=x_predictor)["y"]
            self.posterior_prediction = prediction

        # 事前分布から生成する場合
        elif prediction_type == "prior":
            predictive = Predictive(self.model, num_samples=num_samples)
            prediction = predictive(rng_key, x=x_predictor)["y"]
            self.prior_prediction = prediction

        else:
            raise ValueError("引数prediction_typeには、'prior'か'posterior'を指定してください。")

        # 平均およびHDIをまとめたデータフレームを返す場合
        if return_mean_hdi_df:
            y_predictive_mean = np.mean(prediction, axis=0)
            y_predictive_hdi = az.hdi(
                np.array(prediction)[np.newaxis, :, :], hdi_prob=hdi_prob
            )

            prediction_df = pd.DataFrame(
                {
                    "mean": y_predictive_mean,
                    "lwr": y_predictive_hdi[:, 0],
                    "upr": y_predictive_hdi[:, 1],
                }
            )
            return prediction_df
        else:
            return None

    def outlier_prob(self, x: jnp.array = None, y: jnp.array = None, seed: int = 123):
        rng_key = random.PRNGKey(seed)

        if x == None and y == None:
            x = self.x
            y = self.y

        predictive_discrete = Predictive(
            self.model, self.mcmc_samples, infer_discrete=True
        )
        prediction_discrete = predictive_discrete(rng_key, y=y, x=x)

        outlier_prob = np.mean(prediction_discrete["is_outlier"], axis=0)

        return outlier_prob


# %%
def generate_regression_data(
    n_samples, alpha, beta, sigma, p_outlier=0.01, outlier_scale=10, seed=123
) -> pd.DataFrame:
    """
    線形回帰モデルにしたがう、外れ値を含むデータを生成する

    Parameters:
    n (int): Number of data points.
    alpha (float): Intercept of the linear model.
    beta (float): Slope of the linear model.
    sigma (float): Standard deviation of the noise.
    p_outlier (float): Probability of an outlier.

    Returns:
    pd.DataFrame: A DataFrame with columns 'x' and 'y'.
    """
    # シードを固定
    np.random.seed(seed)

    # betaの次元数を取得
    n_dims = beta.shape[0]

    # 平均0・標準偏差10の正規分布から、説明変数xをn個生成
    x = np.random.normal(50, 10, n_samples * n_dims).reshape(n_samples, n_dims)

    # 誤差項を生成(外れ値なしver)
    errors = np.random.normal(0, sigma, n_samples)

    # 誤差項を生成(外れ値ありver, 標準偏差はoutlier_scale*sigma)
    outliers = np.random.normal(0, outlier_scale * sigma, n_samples)

    # 0~1の一様乱数を生成し、p_outlierより小さいインデックスのサンプルを外れ値として扱う
    is_outlier = np.random.uniform(0, 1, n_samples) < p_outlier
    errors[is_outlier] = outliers[is_outlier]

    # 線形回帰式によって目的変数yを生成
    y = alpha + np.dot(x, beta) + errors

    df_y = pd.DataFrame({"y": y})
    if n_dims == 1:
        df_x = pd.DataFrame(x, columns=["x"])
    else:
        df_x = pd.DataFrame(x, columns=[f"x{i}" for i in range(n_dims)])
    df = pd.concat([df_y, df_x], axis=1)

    return df
