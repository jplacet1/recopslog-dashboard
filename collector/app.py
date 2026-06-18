import requests
import psycopg2
import time
import os
import logging

from datetime import datetime
from dotenv import load_dotenv


load_dotenv()


# ==========================
# LOG CONFIG
# ==========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ==========================
# CONFIG
# ==========================

API_URL = "https://combustivelapi.com.br/api/precos/"


DB_CONFIG = {
    "host": os.getenv("DATABASE_HOST"),
    "port": os.getenv("DATABASE_PORT"),
    "dbname": os.getenv("DATABASE_NAME"),
    "user": os.getenv("DATABASE_USER"),
    "password": os.getenv("DATABASE_PASSWORD")
}



# ==========================
# DATABASE
# ==========================

def conectar():

    logger.info(
        f"Conectando PostgreSQL {DB_CONFIG['host']}:{DB_CONFIG['port']}"
    )

    return psycopg2.connect(**DB_CONFIG)



def salvar_dados(payload):

    inicio = time.time()

    conn = None

    try:

        conn = conectar()

        cursor = conn.cursor()


        logger.info("Inserindo coleta")


        cursor.execute(
            """
            INSERT INTO coletas
            (
                data_coleta,
                fonte,
                moeda,
                tempo_execucao_segundos
            )
            VALUES (%s,%s,%s,%s)
            RETURNING id
            """,

            (
                payload["data_coleta"],
                payload["fonte"],
                payload["moeda"],
                payload["tempo_execucao_segundos"]
            )
        )


        coleta_id = cursor.fetchone()[0]


        total_precos = 0


        logger.info(
            f"Coleta criada ID={coleta_id}"
        )


        for combustivel, estados in payload["precos"].items():

            for estado, preco in estados.items():

                preco = preco.replace(",", ".")


                cursor.execute(
                    """
                    INSERT INTO precos_combustiveis
                    (
                        coleta_id,
                        combustivel,
                        estado,
                        preco
                    )
                    VALUES (%s,%s,%s,%s)
                    """,

                    (
                        coleta_id,
                        combustivel,
                        estado,
                        preco
                    )
                )

                total_precos += 1



        logger.info(
            f"{total_precos} preços inseridos"
        )



        analise = payload["analise"]


        cursor.execute(
            """
            INSERT INTO analises
            (
            coleta_id,

            estado_barato_gasolina,
            preco_barato_gasolina,

            estado_caro_gasolina,
            preco_caro_gasolina,

            diferenca_gasolina,
            variacao_percentual_gasolina,

            estado_barato_diesel,
            preco_barato_diesel,

            estado_caro_diesel,
            preco_caro_diesel,

            diferenca_diesel,
            variacao_percentual_diesel

            )

            VALUES
            (
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s
            )
            """,

            (

            coleta_id,


            analise["estado_mais_barato_gasolina"]["sigla"],
            float(
                analise["estado_mais_barato_gasolina"]["preco"]
                .replace(",", ".")
            ),


            analise["estado_mais_caro_gasolina"]["sigla"],
            float(
                analise["estado_mais_caro_gasolina"]["preco"]
                .replace(",", ".")
            ),


            float(
                analise["diferenca_gasolina"]
                .replace(",", ".")
            ),


            float(
                analise["variacao_percentual_gasolina"]
                .replace("%","")
                .replace(",",".")
            ),



            analise["estado_mais_barato_diesel"]["sigla"],

            float(
                analise["estado_mais_barato_diesel"]["preco"]
                .replace(",", ".")
            ),


            analise["estado_mais_caro_diesel"]["sigla"],


            float(
                analise["estado_mais_caro_diesel"]["preco"]
                .replace(",", ".")
            ),


            float(
                analise["diferenca_diesel"]
                .replace(",", ".")
            ),


            float(
                analise["variacao_percentual_diesel"]
                .replace("%","")
                .replace(",",".")
            )

            )

        )


        conn.commit()


        tempo = round(
            time.time() - inicio,
            2
        )


        logger.info(
            f"Banco atualizado com sucesso em {tempo}s"
        )



    except Exception:

        logger.exception(
            "Erro salvando dados"
        )


        if conn:
            conn.rollback()


    finally:

        if conn:
            conn.close()





# ==========================
# API
# ==========================

def buscar_api():

    inicio = time.time()

    logger.info(
        "Consultando API de combustíveis..."
    )


    try:

        response = requests.get(
            API_URL,
            timeout=20
        )


        logger.info(
            f"API respondeu HTTP {response.status_code}"
        )


        response.raise_for_status()


        dados = response.json()



        if dados.get("error"):

            logger.warning(
                "API retornou erro"
            )

            return



        salvar_dados(dados)



        logger.info(
            "Processo finalizado"
        )



    except Exception:

        logger.exception(
            "Falha na requisição API"
        )





# ==========================
# LOOP
# ==========================


logger.info(
    "🚀 Collector iniciado"
)


while True:


    buscar_api()


    logger.info(
        "Dormindo 1 hora..."
    )


    time.sleep(3600)
